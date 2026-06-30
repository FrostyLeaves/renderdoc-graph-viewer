# -*- coding: utf-8 -*-
"""D3D12 per-API implementation: depth, shader access and Present source."""

from .._sdutil import (_chunk_is, _last_resource_id, _base, _ci, _cstr, _crid,
                       _rid_str)
from ._common import _reflect_verdicts
from ._d3d import _d3d_depth_access


class _D3D12DepthPass(object):
    """Parses the graphics-PSO depth-stencil state. RenderDoc names the chunk
    'CreateGraphicsPipeline' / 'CreateGraphicsPipelineState' for a classic
    CreateGraphicsPipelineState call and 'CreatePipelineState' for a stream-style
    one (modern RHIs / UE5); pDesc.DepthStencilState carries the same field names
    either way."""

    def __init__(self, chunks):
        self.tables = {}
        self.seen = 0
        self.cur = None
        for c in chunks:
            if not (_chunk_is(c.name, 'CreateGraphicsPipeline')
                    or _chunk_is(c.name, 'CreateGraphicsPipelineState')
                    or _chunk_is(c.name, 'CreatePipelineState')):
                continue
            self.seen += 1
            try:
                desc = c.FindChild('pDesc')
                dss = (desc.FindChild('DepthStencilState')
                       if desc is not None else None)
                pid = None
                for nm in ('pPipelineState', 'pPipeline', 'PipelineState'):
                    n = c.FindChild(nm)
                    if n is not None:
                        pid = str(n.AsResourceId())
                        break
                if pid is None:
                    pid = _last_resource_id(c)
                acc = _d3d_depth_access(dss)
                if pid and acc is not None:
                    self.tables[pid] = acc
            except Exception:
                continue

    def on_chunk(self, ch):
        if not _chunk_is(ch.name, 'SetPipelineState'):
            return
        try:
            n = ch.FindChild('pPipelineState')
            self.cur = (str(n.AsResourceId()) if n is not None
                        else _last_resource_id(ch))
        except Exception:
            pass

    def current(self):
        return self.tables.get(self.cur)


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
    """Frame-start D3D12 descriptor heap entries: {(heap, slot): resource}."""
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


class _D3D12RefinePass(object):
    """Zero-replay D3D12 shader-access refinement."""

    def __init__(self, controller, rd, chunks):
        self.controller = controller
        self.rd = rd
        # PSO links come from resource relationships (compute PSO -> CS shader +
        # root signature), read straight off the controller.
        self.pso_links = _d3d12_pso_links(controller)
        self.rootsigs = _d3d12_rootsigs(chunks)

        # Frame-start heap contents, then in-frame CreateUAV/SRV overrides.
        self.desc_res = _d3d12_heap_initial(chunks)   # (heap, index) -> resource
        for c in chunks:
            b = _base(c.name)
            if b in ('CreateUnorderedAccessView', 'CreateShaderResourceView'):
                desc = c.FindChild('desc')
                dst = c.FindChild('dst')
                res = _crid(desc, 'Resource') if desc is not None else None
                heap = _crid(dst, 'heap') if dst is not None else None
                if res and heap:
                    self.desc_res[(heap, _ci(dst, 'index'))] = res

        self.copy_map = {}   # (dstHeap, dstIdx) -> (srcHeap, srcIdx)
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
                    self.copy_map[(dh, _ci(dst, 'index'))] = (sh, _ci(src, 'index'))

        self.refl_cache = {}
        self.cur_pso = None
        self.table_base = {}   # root param index -> (heap, base index)

    def _resolve(self, heap, idx):
        seen = 0
        while (heap, idx) in self.copy_map and seen < 64:
            heap, idx = self.copy_map[(heap, idx)]
            seen += 1
        return self.desc_res.get((heap, idx))

    def on_chunk(self, ch):
        nm = _base(ch.name)
        if nm == 'SetPipelineState':
            self.cur_pso = _crid(ch, 'pPipelineState')
        elif nm == 'SetComputeRootDescriptorTable':
            bd = ch.FindChild('BaseDescriptor')
            if bd is not None:
                self.table_base[_ci(ch, 'RootParameterIndex')] = (
                    _crid(bd, 'heap'), _ci(bd, 'index'))

    def attribute(self, action):
        if not self.cur_pso:
            return []
        link = self.pso_links.get(self.cur_pso)
        params = self.rootsigs.get(link[1]) if link else None
        out = []
        if link and params is not None:
            for (space, reg, acc) in _reflect_verdicts(
                    self.controller, self.rd, self.rd.ResourceId.Null(), link[0],
                    '', 'Compute', self.refl_cache):
                for rpi, ranges in enumerate(params):
                    for (rt, br, nd, ofs, sp) in ranges:
                        if rt != _D3D12_RANGE_UAV or sp != space:
                            continue
                        if not (br <= reg < br + nd):
                            continue
                        base = self.table_base.get(rpi)
                        if not base or not base[0]:
                            continue
                        res = self._resolve(base[0], base[1] + ofs + (reg - br))
                        if res:
                            out.append((res, acc))
        return out


class _D3D12PresentResolver(object):
    """Resolve D3D12 Present source from command-stream evidence."""

    def __init__(self, chunks):
        self.chunks = chunks
        self.swapbuffers = set()
        for ch in chunks:
            if _base(ch.name) != 'GetBuffer':
                continue
            rid = _crid(ch, 'SwapbufferID')
            if rid:
                self.swapbuffers.add(rid)
        self.writes = []
        self._scan_writes()

    def _mark_written(self, chunk_index, resources):
        resources = sorted(set(r for r in resources if r))
        swapbuffers = sorted(r for r in resources if r in self.swapbuffers)
        if resources:
            self.writes.append((chunk_index, resources, swapbuffers))

    def _rtv_resources(self, ch):
        descs = ch.FindChild('pRenderTargetDescriptors')
        out = []
        n = descs.NumChildren() if descs is not None else 0
        for i in range(n):
            rid = _crid(descs.GetChild(i), 'Resource')
            if rid:
                out.append(rid)
        return out

    def _clear_rtv_resource(self, ch):
        desc = ch.FindChild('RenderTargetView')
        if desc is None:
            desc = ch.FindChild('DestRenderTarget')
        return _crid(desc, 'Resource') if desc is not None else None

    def _scan_writes(self):
        bound_rtvs = {}
        draw_chunks = (
            'DrawInstanced', 'DrawIndexedInstanced',
            'ExecuteIndirect')
        for chunk_index, ch in enumerate(self.chunks):
            nm = _base(ch.name)
            cmd = (_crid(ch, 'pCommandList') or
                   _crid(ch, 'CommandList'))
            if nm == 'OMSetRenderTargets':
                if cmd:
                    bound_rtvs[cmd] = self._rtv_resources(ch)
            elif nm in draw_chunks:
                if cmd:
                    self._mark_written(chunk_index, bound_rtvs.get(cmd, ()))
            elif nm == 'ClearRenderTargetView':
                rid = self._clear_rtv_resource(ch)
                if rid:
                    self._mark_written(chunk_index, [rid])

    def _event_presented_image(self, action):
        for ev in getattr(action, 'events', ()):
            try:
                ch = self.chunks[ev.chunkIndex]
            except Exception:
                continue
            if _base(ch.name) != 'End of Capture':
                continue
            rid = _crid(ch, 'PresentedImage')
            if rid:
                return ev.chunkIndex, rid
            return ev.chunkIndex, None
        return None, None

    def _last_unique_write(self, cutoff, use_swapbuffer):
        for chunk_index, rtvs, swapbuffers in reversed(self.writes):
            if cutoff is not None and chunk_index > cutoff:
                continue
            resources = swapbuffers if use_swapbuffer else rtvs
            if len(resources) == 1:
                return resources[0]
            if resources:
                return None
        return None

    def resolve(self, action):
        cutoff, rid = self._event_presented_image(action)
        if rid:
            return rid
        return (self._last_unique_write(cutoff, True) or
                self._last_unique_write(cutoff, False))


API_KEYS = ('d3d12',)
DEPTH_PASS = _D3D12DepthPass
REFINE_PASS = _D3D12RefinePass
PRESENT_RESOLVER = _D3D12PresentResolver
