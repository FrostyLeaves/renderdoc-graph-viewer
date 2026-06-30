# -*- coding: utf-8 -*-
"""D3D11 shader-access reconstruction (apis.d3d11._D3D11RefinePass): map a
dispatch's reflected UAV registers back to the bound resource using immediate-
context state (CreateUAV view->resource + CSSetUnorderedAccessViews slots). The
shader reflection itself is stubbed — it is controller-driven and covered via
the Vulkan descriptor path."""
import types
import unittest

from tests import rd_stub
rd = rd_stub.install()

from renderdoc_graph_viewer.parse.apis import d3d11 as _d3d11
from renderdoc_graph_viewer.parse.apis._common import READ, WRITE, RW


class SDO(object):
    """Minimal fake SDObject: name + children + one resource id / int."""

    def __init__(self, name, kids=None, rid=None, i=None):
        self.name = name
        self._kids = kids or []
        self._rid, self._i = rid, i

    def FindChild(self, n):
        for k in self._kids:
            if k.name == n:
                return k
        return None

    def NumChildren(self):
        return len(self._kids)

    def GetChild(self, idx):
        return self._kids[idx]

    def AsResourceId(self):
        if self._rid is None:
            raise ValueError('not a resource id')
        return self._rid

    def AsInt(self):
        if self._i is None:
            raise ValueError('not an int')
        return self._i


def _create_uav(view, resource):
    return SDO('CreateUnorderedAccessView',
               [SDO('pResource', rid=resource), SDO('pView', rid=view)])


def _cs_set_shader(shader):
    return SDO('CSSetShader', [SDO('pShader', rid=shader)])


def _cs_set_uavs(start, views):
    lst = SDO('ppUnorderedAccessViews', [SDO('$el', rid=v) for v in views])
    return SDO('CSSetUnorderedAccessViews', [SDO('StartSlot', i=start), lst])


class TestD3D11Refine(unittest.TestCase):
    def setUp(self):
        self._orig = _d3d11._reflect_verdicts

    def tearDown(self):
        _d3d11._reflect_verdicts = self._orig

    def _attribute(self, chunks, on_chunks, verdicts):
        # verdicts: [(space, bind, access)] returned by the stubbed reflection
        _d3d11._reflect_verdicts = lambda *a, **k: verdicts
        p = _d3d11._D3D11RefinePass(None, rd, chunks)
        for ch in on_chunks:
            p.on_chunk(ch)
        return p.attribute(types.SimpleNamespace(eventId=1))

    def test_create_uav_builds_view_to_resource(self):
        p = _d3d11._D3D11RefinePass(None, rd, [_create_uav('view::A', 'res::A')])
        self.assertEqual(p.view_res, {'view::A': 'res::A'})

    def test_bound_uav_slot_attributes_to_resource(self):
        out = self._attribute([_create_uav('view::A', 'res::A')],
                              [_cs_set_shader('cs::1'),
                               _cs_set_uavs(0, ['view::A'])],
                              [(0, 0, WRITE)])   # reflection: u0 written
        self.assertEqual(out, [('res::A', WRITE)])

    def test_no_compute_shader_yields_nothing(self):
        out = self._attribute([_create_uav('view::A', 'res::A')],
                              [_cs_set_uavs(0, ['view::A'])],   # no CSSetShader
                              [(0, 0, WRITE)])
        self.assertEqual(out, [])

    def test_register_with_no_bound_slot_is_skipped(self):
        # reflection references u3 but only slot 0 is bound -> dropped
        out = self._attribute([_create_uav('view::A', 'res::A')],
                              [_cs_set_shader('cs::1'),
                               _cs_set_uavs(0, ['view::A'])],
                              [(0, 3, RW)])
        self.assertEqual(out, [])

    def test_start_slot_offsets_the_register(self):
        out = self._attribute([_create_uav('view::B', 'res::B')],
                              [_cs_set_shader('cs::1'),
                               _cs_set_uavs(2, ['view::B'])],   # bound at slot 2
                              [(0, 2, READ)])
        self.assertEqual(out, [('res::B', READ)])

    def test_latest_shader_binding_wins(self):
        out = self._attribute([_create_uav('view::A', 'res::A')],
                              [_cs_set_shader('cs::1'),
                               _cs_set_shader('cs::2'),         # rebind
                               _cs_set_uavs(0, ['view::A'])],
                              [(0, 0, RW)])
        self.assertEqual(out, [('res::A', RW)])


if __name__ == '__main__':
    unittest.main()
