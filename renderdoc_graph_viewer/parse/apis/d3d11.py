# -*- coding: utf-8 -*-
"""D3D11 per-API implementation.

Depth state is a context-bound object. Unbound/NULL means the API default:
test on, write all, func LESS, i.e. read-write.
"""

from .._sdutil import (_chunk_is, _last_resource_id, _rid_str, _base, _ci,
                       _crid, _crid_obj, _self_rid)
from ._common import RW
from ._common import _reflect_verdicts
from ._d3d import _d3d_depth_access


class _D3D11DepthPass(object):
    _DEFAULT = RW

    def __init__(self, chunks):
        self.tables = {}
        self.seen = 0
        self.cur = None
        for c in chunks:
            if not _chunk_is(c.name, 'CreateDepthStencilState'):
                continue
            self.seen += 1
            try:
                desc = c.FindChild('pDepthStencilDesc')
                if desc is None:   # fall back: child carrying the desc fields
                    for i in range(c.NumChildren()):
                        k = c.GetChild(i)
                        if k.FindChild('DepthEnable') is not None:
                            desc = k
                            break
                out = c.FindChild('ppDepthStencilState')
                pid = (str(out.AsResourceId()) if out is not None
                       else _last_resource_id(c))
                acc = _d3d_depth_access(desc)
                if pid and acc is not None:
                    self.tables[pid] = acc
            except Exception:
                continue

    def on_chunk(self, ch):
        if not _chunk_is(ch.name, 'OMSetDepthStencilState'):
            return
        try:
            n = ch.FindChild('pDepthStencilState')
            pid = (str(n.AsResourceId()) if n is not None
                   else _last_resource_id(ch))
            self.cur = _rid_str(pid)
        except Exception:
            pass

    def current(self):
        if self.cur is None:
            return self._DEFAULT
        return self.tables.get(self.cur)


class _D3D11RefinePass(object):
    """Zero-replay D3D11 refinement. No PSO or descriptor heap: UAV/SRV bind to
    immediate-context slots that map HLSL u#/t# registers directly, and
    CreateUAV/SRV give view->resource. Tracks the context's UAV slots and the
    current compute shader, then attributes each dispatch's RW reflection.
    -> {(eid, res_key): READ|WRITE|RW|UNUSED}."""

    def __init__(self, controller, rd, chunks):
        self.controller = controller
        self.rd = rd
        self.view_res = {}
        for c in chunks:
            b = _base(c.name)
            if b in ('CreateUnorderedAccessView', 'CreateShaderResourceView'):
                res = _crid(c, 'pResource')
                view = _crid(c, 'pView')
                if view and res:
                    self.view_res[view] = res
        self.cache = {}
        self.uav_slots = {}   # slot -> view str (immediate-context state, persistent)
        self.cur_cs = None

    def on_chunk(self, ch):
        nm = _base(ch.name)
        if nm == 'CSSetShader':
            self.cur_cs = _crid_obj(ch, 'pShader')
        elif nm == 'CSSetUnorderedAccessViews':
            start = _ci(ch, 'StartSlot')
            lst = ch.FindChild('ppUnorderedAccessViews')
            if lst is not None:
                for k in range(lst.NumChildren()):
                    v = _self_rid(lst.GetChild(k))
                    if v:
                        self.uav_slots[start + k] = v

    def attribute(self, action):
        if self.cur_cs is None:
            return []
        out = []
        for (space, bind, acc) in _reflect_verdicts(
                self.controller, self.rd, self.rd.ResourceId.Null(), self.cur_cs,
                'main', 'Compute', self.cache):
            res = self.view_res.get(self.uav_slots.get(bind))
            if res:
                out.append((res, acc))
        return out


API_KEYS = ('d3d11',)
DEPTH_PASS = _D3D11DepthPass
REFINE_PASS = _D3D11RefinePass
