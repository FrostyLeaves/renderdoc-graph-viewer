# -*- coding: utf-8 -*-
"""Static, zero-replay refinement of read-write (UAV / storage) access direction,
reconstructed from structured-data chunks instead of a per-event SetFrameEvent
walk (~86ms/event). Same spirit as the depth-state adapters.

Pipeline (all zero-replay, validated on a real Vulkan capture):
  PSO -> shaderModule         (pipeline-creation chunks)
  shaderModule -> reflection  (controller.GetShader, cached; gives rawBytes +
                               readWriteResources' (set,binding))
  reflection.rawBytes -> r/w  (shader_access SPIR-V/DXBC/DXIL parser)
  (set,binding) -> resource   (Initial Contents snapshot + in-frame updates +
                               imageView->image + per-command-buffer binds)
A binding the shader parses cleanly but never accesses becomes 'unused' (the
caller dashes its read edge). Anything not statically reconstructable (parse
UNKNOWN, unresolved binding, secondary-buffer ambiguity) is omitted, so the
caller keeps the conservative RW double edge. Vulkan, D3D11 and D3D12 are
implemented (see _REFINERS); other APIs return {}.
"""

from . import shader_access
from ._sdutil import (_base, _ci, _cstr, _crid_obj, _rid_str, _crid,
                      _self_rid)

READ = 'read'
WRITE = 'write'
RW = 'rw'
UNUSED = 'unused'   # shader declared the binding but never accessed it (parse OK)


def _merge(cur, a):
    if cur is None or cur == a:
        return a
    if cur == UNUSED:   # unused is the weakest signal: any real access wins
        return a
    if a == UNUSED:
        return cur
    return RW


# Vulkan shader-stage flag -> graph_model stage prefix / rd.ShaderStage name
_VK_STAGE = {
    'VK_SHADER_STAGE_VERTEX_BIT': 'Vertex',
    'VK_SHADER_STAGE_TESSELLATION_CONTROL_BIT': 'Hull',
    'VK_SHADER_STAGE_TESSELLATION_EVALUATION_BIT': 'Domain',
    'VK_SHADER_STAGE_GEOMETRY_BIT': 'Geometry',
    'VK_SHADER_STAGE_FRAGMENT_BIT': 'Pixel',
    'VK_SHADER_STAGE_COMPUTE_BIT': 'Compute',
}


def _vk_build_set_contents(chunks):
    """-> {descriptorSet(str): {binding(int): set(resource str)}} reconstructed
    from the pre-capture Initial Contents snapshot + in-frame descriptor updates,
    with imageView/bufferView resolved to the underlying image/buffer."""
    view_res = {}
    layout_binds = {}    # layout(str) -> ordered [(binding, count)]
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

    def res(rid):
        return view_res.get(rid, rid)

    def binding_offset(lid, binding):
        off = 0
        for entry in layout_binds.get(lid, []):
            bb, cnt = entry[0], entry[1]
            if bb == binding:
                return off, cnt
            off += cnt
        return None, 0

    set_bind = {}
    for c in chunks:
        b = _base(c.name)
        if b == 'Initial Contents':
            if _cstr(c, 'type') != 'eResDescriptorSet':
                continue
            setid = _crid(c, 'id')
            binds = c.FindChild('Bindings')
            if setid is None or binds is None:
                continue
            flat = [res(_crid(binds.GetChild(i), 'resource'))
                    for i in range(binds.NumChildren())]
            lid = set_layout.get(setid)
            d = set_bind.setdefault(setid, {})
            for entry in layout_binds.get(lid, []):
                bb, cnt = entry[0], entry[1]
                off, _n = binding_offset(lid, bb)
                if off is None:
                    continue
                for k in range(cnt):
                    if off + k < len(flat) and flat[off + k]:
                        d.setdefault(bb, set()).add(flat[off + k])
        elif b in ('vkUpdateDescriptorSets',
                   'vkUpdateDescriptorSetWithTemplate',
                   'vkCmdPushDescriptorSetKHR'):
            wl = c.FindChild('Decoded Writes') or c.FindChild('pDescriptorWrites')
            if wl is None:
                continue
            tmpl = _crid(c, 'descriptorSet')
            for i in range(wl.NumChildren()):
                w = wl.GetChild(i)
                setid = tmpl or _crid(w, 'dstSet')
                if setid is None:
                    continue
                # One write's descriptors fill CONSECUTIVE same-type bindings
                # from (dstBinding, dstArrayElement): a descriptorCount that
                # overflows a binding's count spills into the next binding
                # (VkWriteDescriptorSet rule). A write has one descriptor type,
                # so exactly one array is populated; collect resources in one
                # flat list (shared counter), then map each to its binding via
                # the layout. Distributing all onto dstBinding would collapse a
                # 4-binding template update onto binding 0.
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
                            resources.append(res(rid))
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


def _disasm_target(controller, enc):
    """Disassembly target string for an encoding: the explicit DXIL view when
    present, else the native default. SPIR-V never reaches here (it is binary)."""
    try:
        targets = controller.GetDisassemblyTargets(True)
    except Exception:
        return ''
    if not targets:
        return ''
    if enc == 'DXIL':
        for t in targets:
            if 'DXIL' in t:
                return t
    return targets[0]


def _reflect_verdicts(controller, rd, pipe_obj, shader_obj, entry, stage_name, cache):
    """GetShader reflection -> [(space, binding, access)] for read-write
    resources, classified from the shader's own encoding: binary encodings
    (SPIR-V) feed rawBytes, disassembly encodings (DXBC/DXIL) feed
    DisassembleShader. A declared binding the parser proves untouched is reported
    UNUSED; one it cannot attribute is omitted (caller keeps the conservative RW
    double edge). Cached per shader object (stable across events)."""
    key = str(shader_obj)
    if key in cache:
        return cache[key]
    out = []
    try:
        st = getattr(rd.ShaderStage, stage_name, rd.ShaderStage.Compute)
        refl = controller.GetShader(pipe_obj, shader_obj,
                                    rd.ShaderEntryPoint(entry, st))
    except Exception:
        refl = None
    rwres = getattr(refl, 'readWriteResources', None) if refl else None
    if rwres:
        rwb = [{'index': i, 'bind': r.fixedBindNumber,
                'space': r.fixedBindSetOrSpace} for i, r in enumerate(rwres)]
        enc = str(refl.encoding).split('.')[-1]
        kind_fn = shader_access.PARSERS.get(enc)
        payload = None
        if kind_fn is not None:
            try:
                if kind_fn[0] == 'binary':
                    payload = bytes(refl.rawBytes)
                else:
                    payload = controller.DisassembleShader(
                        pipe_obj, refl, _disasm_target(controller, enc))
            except Exception:
                payload = None
        verdicts = shader_access.parse(enc, payload, rwb) if payload else {}
        for i, r in enumerate(rwres):
            a = verdicts.get(i)
            if a in (READ, WRITE, RW):
                out.append((r.fixedBindSetOrSpace, r.fixedBindNumber, a))
            elif a is None and verdicts:
                out.append((r.fixedBindSetOrSpace, r.fixedBindNumber, UNUSED))
            # a == UNKNOWN: touched but unattributable -> omit (conservative)
    cache[key] = out
    return out


def _walk_executables(controller, rd, setup):
    """Shared per-API refiner skeleton. Walk the action tree in event order,
    feeding each event's chunk (by base name) to on_chunk to track bind state;
    on every executable action (Drawcall|Dispatch) fold attribute(a)'s yielded
    (res_key, access) pairs into {(eid, res_key): merged}. setup(chunks) builds
    the per-API maps and returns (on_chunk, attribute). Returns {} when the
    structured file / root actions are unavailable. _merge is a small join
    lattice (order-independent), so the fold needs no fixed visit order."""
    try:
        chunks = controller.GetStructuredFile().chunks
        roots = controller.GetRootActions()
    except Exception:
        return {}
    on_chunk, attribute = setup(chunks)
    f_exec = rd.ActionFlags.Drawcall | rd.ActionFlags.Dispatch
    result = {}

    def visit(acts):
        for a in acts:
            for ev in a.events:
                try:
                    ch = chunks[ev.chunkIndex]
                except Exception:
                    continue
                on_chunk(_base(ch.name), ch)
            if a.flags & f_exec:
                for (res, acc) in attribute(a):
                    k = (a.eventId, res)
                    result[k] = _merge(result.get(k), acc)
            visit(a.children)

    visit(roots)
    return result


def vk_refine(controller, rd):
    """Zero-replay -> {(eid, res_key): 'read'|'write'|'rw'|'unused'}. 'unused' =
    shader declared the binding but never accessed it (parse succeeded). Omitted
    entries (parse UNKNOWN / unresolved binding) keep the conservative RW double
    edge in the caller."""
    def setup(chunks):
        set_bind = _vk_build_set_contents(chunks)
        psos = _vk_pso_shaders(chunks)
        refl_cache = {}   # shader(str) -> [(set, binding, access)]
        bound = {}     # cmdbuf -> {set_index: descriptorSet str}
        cur_pso = {}   # cmdbuf -> pipeline str

        def cmdbuf_of(a):
            for ev in a.events:
                try:
                    ch = chunks[ev.chunkIndex]
                except Exception:
                    continue
                nm = _base(ch.name)
                if nm.startswith('vkCmdDispatch') or nm.startswith('vkCmdDraw'):
                    return _crid(ch, 'commandBuffer')
            return None

        def on_chunk(nm, ch):
            cb = _crid(ch, 'commandBuffer')
            if nm == 'vkBeginCommandBuffer' and cb:
                bound[cb] = {}
                cur_pso.pop(cb, None)
            elif nm == 'vkCmdBindDescriptorSets' and cb:
                first = _ci(ch, 'firstSet')
                ps = ch.FindChild('pDescriptorSets')
                if ps is not None:
                    d = bound.setdefault(cb, {})
                    for k in range(ps.NumChildren()):
                        sid = _self_rid(ps.GetChild(k))
                        if sid:
                            d[first + k] = sid
            elif nm == 'vkCmdBindPipeline' and cb:
                cur_pso[cb] = _crid(ch, 'pipeline')

        def attribute(a):
            cb = cmdbuf_of(a)
            sets = bound.get(cb, {})
            stages = psos.get(cur_pso.get(cb))
            out = []
            if stages:
                for stage_pre, (mod, pobj, entry) in stages.items():
                    if not mod:
                        continue
                    for (s, bind, acc) in _reflect_verdicts(
                            controller, rd, pobj, mod, entry, stage_pre,
                            refl_cache):
                        ds = sets.get(s)
                        for r in set_bind.get(ds, {}).get(bind, set()):
                            out.append((r, acc))
            return out

        return on_chunk, attribute

    return _walk_executables(controller, rd, setup)


def d3d11_refine(controller, rd):
    """Zero-replay D3D11 refinement. No PSO or descriptor heap: UAV/SRV bind to
    immediate-context slots that map HLSL u#/t# registers directly, and
    CreateUAV/SRV give view->resource. Tracks the context's UAV slots and the
    current compute shader, then attributes each dispatch's RW reflection.
    -> {(eid, res_key): READ|WRITE|RW|UNUSED}."""
    def setup(chunks):
        view_res = {}
        for c in chunks:
            b = _base(c.name)
            if b in ('CreateUnorderedAccessView', 'CreateShaderResourceView'):
                res = _crid(c, 'pResource')
                view = _crid(c, 'pView')
                if view and res:
                    view_res[view] = res
        cache = {}
        uav_slots = {}      # slot -> view str (immediate-context state, persistent)
        cur_cs = [None]

        def on_chunk(nm, ch):
            if nm == 'CSSetShader':
                cur_cs[0] = _crid_obj(ch, 'pShader')
            elif nm == 'CSSetUnorderedAccessViews':
                start = _ci(ch, 'StartSlot')
                lst = ch.FindChild('ppUnorderedAccessViews')
                if lst is not None:
                    for k in range(lst.NumChildren()):
                        v = _self_rid(lst.GetChild(k))
                        if v:
                            uav_slots[start + k] = v

        def attribute(a):
            if cur_cs[0] is None:
                return []
            out = []
            for (space, bind, acc) in _reflect_verdicts(
                    controller, rd, rd.ResourceId.Null(), cur_cs[0],
                    'main', 'Compute', cache):
                res = view_res.get(uav_slots.get(bind))
                if res:
                    out.append((res, acc))
            return out

        return on_chunk, attribute

    return _walk_executables(controller, rd, setup)


_D3D12_RANGE_UAV = 1   # D3D12_DESCRIPTOR_RANGE_TYPE_UAV
_D3D12_APPEND = 0xFFFFFFFF   # D3D12_DESCRIPTOR_RANGE_OFFSET_APPEND


def _d3d12_pso_links(controller):
    """{pso(str): (cs_shader_obj, rootsig(str))} from resource relationships.
    A compute PSO's parentResources lists its Shader and its ShaderBinding (root
    signature) -- both zero-replay, no per-event pipeline state needed."""
    rtype = {}
    for rr in controller.GetResources():
        rtype[str(rr.resourceId)] = str(rr.type)
    out = {}
    for rr in controller.GetResources():
        if str(rr.type) != 'ResourceType.PipelineState':
            continue
        cs, rs = None, None
        for p in (getattr(rr, 'parentResources', None) or []):
            t = rtype.get(str(p))
            if t == 'ResourceType.Shader':
                cs = p
            elif t == 'ResourceType.ShaderBinding':
                rs = str(p)
        if cs is not None:
            out[str(rr.resourceId)] = (cs, rs)
    return out


def _d3d12_heap_initial(chunks):
    """Frame-start descriptor-heap snapshot from D3D12 'Initial Contents' chunks:
    {(heap, slot): resource} for descriptors that existed before the captured
    frame. RenderDoc records each shader-visible heap's contents at frame start as
    an 'Initial Contents' chunk (type 'Descriptor Heap') with a Descriptors array;
    each entry carries its slot, view type and the resource it points at. Only UAV
    slots are kept -- the refiner resolves UAV registers only. This is the D3D12
    analogue of the Vulkan descriptor-set Initial Contents path."""
    out = {}
    for c in chunks:
        if _base(c.name) != 'Initial Contents' or _cstr(c, 'type') != 'Descriptor Heap':
            continue
        heap = _crid(c, 'id')
        descs = c.FindChild('Descriptors')
        n = descs.NumChildren() if descs is not None else 0
        if heap is None or n == 0:
            continue
        # per-slot record is {type, heap, index, Resource, Descriptor}; read it
        # positionally (this runs over 100k+ slots) after validating the layout on
        # the first element, falling back to named lookup if it differs.
        f = descs.GetChild(0)
        pos = (f.NumChildren() >= 4 and f.GetChild(0).name == 'type' and
               f.GetChild(2).name == 'index' and f.GetChild(3).name == 'Resource')
        for i in range(n):
            el = descs.GetChild(i)
            if pos:
                if el.GetChild(0).AsString() != 'UAV':
                    continue
                res = _rid_str(el.GetChild(3).AsResourceId())
                idx = el.GetChild(2).AsInt()
            else:
                if _cstr(el, 'type') != 'UAV':
                    continue
                res = _crid(el, 'Resource')
                idx = _ci(el, 'index', i)
            if res:
                out[(heap, idx)] = res
    return out


def _d3d12_rootsigs(chunks):
    """{rootsig(str): [per-root-param [(rangeType, baseReg, count, offset, space)]]}
    from CreateRootSignature's UnpackedSignature (RenderDoc pre-parses the blob)."""
    out = {}
    for c in chunks:
        if _base(c.name) != 'CreateRootSignature':
            continue
        rsid = _crid(c, 'pRootSignature')
        us = c.FindChild('UnpackedSignature')
        params = us.FindChild('Parameters') if us is not None else None
        if not rsid or params is None:
            continue
        plist = []
        for i in range(params.NumChildren()):
            dt = params.GetChild(i).FindChild('DescriptorTable')
            ranges = []
            running = 0
            rngs = dt.FindChild('pDescriptorRanges') if dt is not None else None
            if rngs is not None:
                for j in range(rngs.NumChildren()):
                    rg = rngs.GetChild(j)
                    ofs = _ci(rg, 'OffsetInDescriptorsFromTableStart')
                    if ofs == _D3D12_APPEND:
                        ofs = running
                    nd = _ci(rg, 'NumDescriptors')
                    ranges.append((_ci(rg, 'RangeType'), _ci(rg, 'BaseShaderRegister'),
                                   nd, ofs, _ci(rg, 'RegisterSpace')))
                    running = ofs + nd
            plist.append(ranges)
        out[rsid] = plist
    return out


def d3d12_refine(controller, rd):
    """Zero-replay D3D12 refinement. Descriptors live in heaps addressed by
    (heap, index); the reconstruction is: PSO -> CS shader + root signature
    (resource relationships); CS reflection (GetShader, no replay); root-sig
    range maps a register to a table offset; SetComputeRootDescriptorTable gives
    the table's base heap index; CopyDescriptors are followed back to the
    CreateUAV/SRV that wrote each (heap, index) -> resource. A register the root
    signature does not cover (bindless / vendor-extension UAV slots) is omitted,
    so the caller keeps the conservative RW double edge.
    -> {(eid, res_key): READ|WRITE|RW|UNUSED}."""
    def setup(chunks):
        pso_links = _d3d12_pso_links(controller)
        rootsigs = _d3d12_rootsigs(chunks)

        # Frame-START heap contents first, then in-frame CreateUAV/SRV override.
        # Persistent heaps (e.g. UE5) create their descriptors BEFORE the captured
        # frame and copy them in each frame, so the in-frame CreateUAV/SRV chunks
        # miss almost everything; without the Initial-Contents snapshot resolution
        # yields ~0.
        desc_res = _d3d12_heap_initial(chunks)   # (heap, index) -> resource
        for c in chunks:
            b = _base(c.name)
            if b in ('CreateUnorderedAccessView', 'CreateShaderResourceView'):
                desc = c.FindChild('desc')
                dst = c.FindChild('dst')
                res = _crid(desc, 'Resource') if desc is not None else None
                heap = _crid(dst, 'heap') if dst is not None else None
                if res and heap:
                    desc_res[(heap, _ci(dst, 'index'))] = res

        copy_map = {}   # (dstHeap, dstIdx) -> (srcHeap, srcIdx)
        for c in chunks:
            if _base(c.name) not in ('CopyDescriptorsSimple', 'CopyDescriptors'):
                continue
            dc = c.FindChild('DescriptorCopies')
            if dc is None:
                continue
            for i in range(dc.NumChildren()):
                el = dc.GetChild(i)
                dst, src = el.FindChild('dst'), el.FindChild('src')
                if dst is None or src is None:
                    continue
                dh, sh = _crid(dst, 'heap'), _crid(src, 'heap')
                if dh and sh:
                    copy_map[(dh, _ci(dst, 'index'))] = (sh, _ci(src, 'index'))

        def resolve(heap, idx):
            seen = 0
            while (heap, idx) in copy_map and seen < 64:
                heap, idx = copy_map[(heap, idx)]
                seen += 1
            return desc_res.get((heap, idx))

        refl_cache = {}
        cur_pso = [None]
        table_base = {}   # root param index -> (heap, base index)

        def on_chunk(nm, ch):
            if nm == 'SetPipelineState':
                cur_pso[0] = _crid(ch, 'pPipelineState')
            elif nm == 'SetComputeRootDescriptorTable':
                bd = ch.FindChild('BaseDescriptor')
                if bd is not None:
                    table_base[_ci(ch, 'RootParameterIndex')] = (
                        _crid(bd, 'heap'), _ci(bd, 'index'))

        def attribute(a):
            if not cur_pso[0]:
                return []
            link = pso_links.get(cur_pso[0])
            params = rootsigs.get(link[1]) if link else None
            out = []
            if link and params is not None:
                for (space, reg, acc) in _reflect_verdicts(
                        controller, rd, rd.ResourceId.Null(), link[0],
                        '', 'Compute', refl_cache):
                    for rpi, ranges in enumerate(params):
                        for (rt, br, nd, ofs, sp) in ranges:
                            if rt != _D3D12_RANGE_UAV or sp != space:
                                continue
                            if not (br <= reg < br + nd):
                                continue
                            base = table_base.get(rpi)
                            if not base or not base[0]:
                                continue
                            res = resolve(base[0], base[1] + ofs + (reg - br))
                            if res:
                                out.append((res, acc))
            return out

        return on_chunk, attribute

    return _walk_executables(controller, rd, setup)


_REFINERS = {
    'vulkan': vk_refine,
    'd3d11': d3d11_refine,
    'd3d12': d3d12_refine,
}


def refine(controller, rd, warnings=None):
    """Dispatch to the per-API static refiner -> {(eid, res_key):
    READ|WRITE|RW|UNUSED}. Zero replay. Returns {} - and the caller keeps the
    conservative RW double edge - for an API with no refiner (e.g. GL) or a
    frame with nothing refinable. warnings: optional list; a genuine refiner
    exception appends a diagnostic here so the caller can tell a real parse
    failure apart from a legitimately empty result (mirrors the depth pass)."""
    try:
        api = str(controller.GetAPIProperties().pipelineType).split('.')[-1].lower()
    except Exception:
        return {}
    fn = _REFINERS.get(api)
    if fn is None:
        return {}
    try:
        return fn(controller, rd)
    except Exception as exc:
        if warnings is not None:
            warnings.append(
                'shader-access refinement failed for %s (%s: %s); bindings '
                'keep conservative read+write edges'
                % (api, type(exc).__name__, exc))
        return {}
