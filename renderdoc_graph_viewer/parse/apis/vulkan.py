# -*- coding: utf-8 -*-
"""Vulkan per-API implementation: depth and shader-access refinement."""

from .._sdutil import _base, _ci, _cstr, _crid, _crid_obj, _self_rid
from ._common import ACCESS_NONE, combine_depth_access
from ._common import _reflect_verdicts

# Vulkan enum values (stable per the VK spec)
_VK_COMPARE_OP_ALWAYS = 7
_VK_STENCIL_OP_KEEP = 0
_VK_BIND_POINT_GRAPHICS = 0


def _pipeline_depth_access(dss):
    """Classify one VkPipelineDepthStencilStateCreateInfo into read/write/rw/none."""
    if dss is None or dss.NumChildren() == 0:
        return ACCESS_NONE
    reads = False
    writes = False
    if _ci(dss, 'depthTestEnable'):
        if _ci(dss, 'depthCompareOp') != _VK_COMPARE_OP_ALWAYS:
            reads = True
        if _ci(dss, 'depthWriteEnable'):
            writes = True
    if _ci(dss, 'stencilTestEnable'):
        for fname in ('front', 'back'):
            f = dss.FindChild(fname)
            if f is None:
                continue
            if _ci(f, 'compareOp') != _VK_COMPARE_OP_ALWAYS:
                reads = True
            wm = _ci(f, 'writeMask')
            ops = (_ci(f, 'failOp'), _ci(f, 'passOp'),
                   _ci(f, 'depthFailOp'))
            if wm and any(o != _VK_STENCIL_OP_KEEP for o in ops):
                writes = True
    return combine_depth_access(reads, writes)


class _VkDepthPass(object):
    def __init__(self, chunks):
        self.tables = {}
        self.seen = 0
        self.cur = None
        for c in chunks:
            if c.name != 'vkCreateGraphicsPipelines':
                continue
            self.seen += 1
            try:
                pid = str(c.FindChild('Pipeline').AsResourceId())
                info = c.FindChild('CreateInfo')
                dss = (info.FindChild('pDepthStencilState')
                       if info is not None else None)
                self.tables[pid] = _pipeline_depth_access(dss)
            except Exception:
                continue

    def on_chunk(self, ch):
        if ch.name != 'vkCmdBindPipeline':
            return
        try:
            if (ch.FindChild('pipelineBindPoint').AsInt() ==
                    _VK_BIND_POINT_GRAPHICS):
                self.cur = str(ch.FindChild('pipeline').AsResourceId())
        except Exception:
            pass

    def current(self):
        return self.tables.get(self.cur)


# Vulkan shader-stage flag -> graph_model stage prefix / rd.ShaderStage name
_VK_STAGE = {
    'VK_SHADER_STAGE_VERTEX_BIT': 'Vertex',
    'VK_SHADER_STAGE_TESSELLATION_CONTROL_BIT': 'Hull',
    'VK_SHADER_STAGE_TESSELLATION_EVALUATION_BIT': 'Domain',
    'VK_SHADER_STAGE_GEOMETRY_BIT': 'Geometry',
    'VK_SHADER_STAGE_FRAGMENT_BIT': 'Pixel',
    'VK_SHADER_STAGE_COMPUTE_BIT': 'Compute',
}


def _vk_binding_offset(layout_binds, lid, binding):
    """Flat offset + count of `binding` within its set layout's bindings."""
    off = 0
    for entry in layout_binds.get(lid, []):
        bb, cnt = entry[0], entry[1]
        if bb == binding:
            return off, cnt
        off += cnt
    return None, 0


def _vk_collect_views_and_layouts(chunks):
    """First pass: imageView/bufferView -> underlying image/buffer, descriptor-
    set-layout bindings, and set -> layout. Returns
    (view_res, layout_binds, set_layout)."""
    view_res = {}
    layout_binds = {}    # layout(str) -> ordered [(binding, count, type)]
    set_layout = {}      # set(str) -> layout(str)
    for c in chunks:
        b = _base(c.name)
        if b in ('vkCreateImageView', 'vkCreateBufferView'):
            ci = c.FindChild('CreateInfo')
            src = (_crid(ci, 'image') or _crid(ci, 'buffer')) if ci else None
            view = _crid(c, 'View')
            if view and src:
                view_res[view] = src
        elif b == 'vkCreateDescriptorSetLayout':
            lid = _crid(c, 'SetLayout')
            ci = c.FindChild('CreateInfo')
            pb = ci.FindChild('pBindings') if ci is not None else None
            binds = []
            if pb is not None:
                for i in range(pb.NumChildren()):
                    el = pb.GetChild(i)
                    binds.append((_ci(el, 'binding'),
                                  _ci(el, 'descriptorCount', 1),
                                  _cstr(el, 'descriptorType')))
            binds.sort()
            if lid:
                layout_binds[lid] = binds
        elif b == 'vkAllocateDescriptorSets':
            setid = _crid(c, 'DescriptorSet')
            ai = c.FindChild('AllocateInfo')
            pl = ai.FindChild('pSetLayouts') if ai is not None else None
            if setid and pl is not None and pl.NumChildren() > 0:
                lid = _self_rid(pl.GetChild(0))
                if lid:
                    set_layout[setid] = lid
    return view_res, layout_binds, set_layout


def _vk_apply_initial_contents(c, view_res, layout_binds, set_layout, set_bind):
    """Fold one 'Initial Contents' (eResDescriptorSet) snapshot chunk into
    set_bind, mapping each flat slot to its binding via the set layout."""
    setid = _crid(c, 'id')
    binds = c.FindChild('Bindings')
    if setid is None or binds is None:
        return
    flat = []
    for i in range(binds.NumChildren()):
        rid = _crid(binds.GetChild(i), 'resource')
        flat.append(view_res.get(rid, rid))
    lid = set_layout.get(setid)
    d = set_bind.setdefault(setid, {})
    for entry in layout_binds.get(lid, []):
        bb, cnt = entry[0], entry[1]
        off, _n = _vk_binding_offset(layout_binds, lid, bb)
        if off is None:
            continue
        for k in range(cnt):
            if off + k < len(flat) and flat[off + k]:
                d.setdefault(bb, set()).add(flat[off + k])


def _vk_apply_descriptor_write(c, view_res, layout_binds, set_layout, set_bind):
    """Fold one descriptor-update chunk (vkUpdateDescriptorSets / template /
    push) into set_bind. One write's descriptors fill CONSECUTIVE same-type
    bindings from (dstBinding, dstArrayElement); a descriptorCount that
    overflows a binding's count spills into the next (VkWriteDescriptorSet
    rule)."""
    wl = c.FindChild('Decoded Writes') or c.FindChild('pDescriptorWrites')
    if wl is None:
        return
    tmpl = _crid(c, 'descriptorSet')
    for i in range(wl.NumChildren()):
        w = wl.GetChild(i)
        setid = tmpl or _crid(w, 'dstSet')
        if setid is None:
            continue
        # A write has one descriptor type, so exactly one array is populated;
        # collect resources in one flat list (shared counter), then map each to
        # its binding via the layout. Distributing all onto dstBinding would
        # collapse a 4-binding template update onto binding 0.
        resources = []
        for arr, fld in (('pBufferInfo', 'buffer'),
                         ('pImageInfo', 'imageView'),
                         ('pTexelBufferView', None)):
            a = w.FindChild(arr)
            if a is None:
                continue
            for j in range(a.NumChildren()):
                el = a.GetChild(j)
                rid = _self_rid(el) if fld is None else _crid(el, fld)
                if rid:
                    resources.append(view_res.get(rid, rid))
        if not resources:
            continue
        dst_bind = _ci(w, 'dstBinding')
        dst_elem = _ci(w, 'dstArrayElement')
        slot_binding = []
        start_type = None
        for entry in layout_binds.get(set_layout.get(setid), []):
            bb, cnt = entry[0], entry[1]
            typ = entry[2] if len(entry) > 2 else ''
            if bb < dst_bind:
                continue
            if start_type is None:
                start_type = typ
            elif typ and start_type and typ != start_type:
                break   # overflow stops at a different descriptor type
            start = dst_elem if bb == dst_bind else 0
            slot_binding.extend([bb] * max(0, cnt - start))
            if len(slot_binding) >= len(resources):
                break
        d = set_bind.setdefault(setid, {})
        for j, rid in enumerate(resources):
            if j < len(slot_binding):
                bind = slot_binding[j]
            elif slot_binding:
                bind = slot_binding[-1]   # short layout: clamp to last real binding
            elif j == 0:
                bind = dst_bind           # no layout, single resource: safe
            else:
                continue                  # no layout, spill: omit (conservative)
            d.setdefault(bind, set()).add(rid)


def _vk_build_set_contents(chunks):
    """-> {descriptorSet(str): {binding(int): set(resource str)}} reconstructed
    from the pre-capture Initial Contents snapshot + in-frame descriptor updates,
    with imageView/bufferView resolved to the underlying image/buffer."""
    view_res, layout_binds, set_layout = _vk_collect_views_and_layouts(chunks)
    set_bind = {}
    for c in chunks:
        b = _base(c.name)
        if b == 'Initial Contents':
            if _cstr(c, 'type') != 'eResDescriptorSet':
                continue
            _vk_apply_initial_contents(
                c, view_res, layout_binds, set_layout, set_bind)
        elif b in ('vkUpdateDescriptorSets',
                   'vkUpdateDescriptorSetWithTemplate',
                   'vkCmdPushDescriptorSetKHR'):
            _vk_apply_descriptor_write(
                c, view_res, layout_binds, set_layout, set_bind)
    return set_bind


def _vk_pso_shaders(chunks):
    """pipeline(str) -> {stage_prefix: (module ResourceId obj, pso obj, entry)}."""
    out = {}
    for c in chunks:
        b = _base(c.name)
        if b == 'vkCreateComputePipelines':
            pid = _crid(c, 'Pipeline')
            pobj = _crid_obj(c, 'Pipeline')
            st = c.FindChild('CreateInfo')
            st = st.FindChild('stage') if st is not None else None
            if pid and st is not None:
                out[pid] = {'Compute': (_crid_obj(st, 'module'), pobj,
                                        _cstr(st, 'pName') or 'main')}
        elif b == 'vkCreateGraphicsPipelines':
            pid = _crid(c, 'Pipeline')
            pobj = _crid_obj(c, 'Pipeline')
            ci = c.FindChild('CreateInfo')
            ps = ci.FindChild('pStages') if ci is not None else None
            if pid and ps is not None:
                stages = {}
                for i in range(ps.NumChildren()):
                    el = ps.GetChild(i)
                    pre = _VK_STAGE.get(_cstr(el, 'stage'))
                    if pre:
                        stages[pre] = (_crid_obj(el, 'module'), pobj,
                                       _cstr(el, 'pName') or 'main')
                if stages:
                    out[pid] = stages
    return out


class _VkRefinePass(object):
    """Zero-replay Vulkan shader-access refinement."""

    def __init__(self, controller, rd, chunks):
        self.controller = controller
        self.rd = rd
        self.chunks = chunks
        self.set_bind = _vk_build_set_contents(chunks)
        self.psos = _vk_pso_shaders(chunks)
        self.refl_cache = {}   # shader(str) -> [(set, binding, access)]
        self.bound = {}     # cmdbuf -> {set_index: descriptorSet str}
        self.cur_pso = {}   # cmdbuf -> pipeline str

    def _cmdbuf_of(self, a):
        for ev in a.events:
            try:
                ch = self.chunks[ev.chunkIndex]
            except Exception:
                continue
            nm = _base(ch.name)
            if nm.startswith('vkCmdDispatch') or nm.startswith('vkCmdDraw'):
                return _crid(ch, 'commandBuffer')
        return None

    def on_chunk(self, ch):
        nm = _base(ch.name)
        cb = _crid(ch, 'commandBuffer')
        if nm == 'vkBeginCommandBuffer' and cb:
            self.bound[cb] = {}
            self.cur_pso.pop(cb, None)
        elif nm == 'vkCmdBindDescriptorSets' and cb:
            first = _ci(ch, 'firstSet')
            ps = ch.FindChild('pDescriptorSets')
            if ps is not None:
                d = self.bound.setdefault(cb, {})
                for k in range(ps.NumChildren()):
                    sid = _self_rid(ps.GetChild(k))
                    if sid:
                        d[first + k] = sid
        elif nm == 'vkCmdBindPipeline' and cb:
            self.cur_pso[cb] = _crid(ch, 'pipeline')

    def attribute(self, action):
        cb = self._cmdbuf_of(action)
        sets = self.bound.get(cb, {})
        stages = self.psos.get(self.cur_pso.get(cb))
        out = []
        if stages:
            for stage_pre, (mod, pobj, entry) in stages.items():
                if not mod:
                    continue
                for (s, bind, acc) in _reflect_verdicts(
                        self.controller, self.rd, pobj, mod, entry, stage_pre,
                        self.refl_cache):
                    ds = sets.get(s)
                    for r in self.set_bind.get(ds, {}).get(bind, set()):
                        out.append((r, acc))
        return out


API_KEYS = ('vulkan',)
DEPTH_PASS = _VkDepthPass
REFINE_PASS = _VkRefinePass
