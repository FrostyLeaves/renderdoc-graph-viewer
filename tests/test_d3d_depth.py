# -*- coding: utf-8 -*-
"""D3D depth/stencil classification (_d3d._d3d_depth_access), zero-replay.

Closes the headless gap on the shared D3D depth helper, including the DESC2
per-face vs classic top-level stencil-write-mask split."""
import unittest

from renderdoc_graph_viewer.parse.apis._d3d import _d3d_depth_access
from renderdoc_graph_viewer.parse.apis._common import READ, WRITE, RW, ACCESS_NONE

ALWAYS = 8   # D3D11/12_COMPARISON_FUNC_ALWAYS
LESS = 2
KEEP = 1     # D3D11/12_STENCIL_OP_KEEP
REPLACE = 3


class _SD(object):
    """Minimal SDObject: named children with int values."""

    def __init__(self, name, value=None, children=None):
        self.name = name
        self._v = value
        self._kids = list(children or [])

    def FindChild(self, name):
        for k in self._kids:
            if k.name == name:
                return k
        return None

    def AsInt(self):
        return int(self._v)


def _dss(pairs, faces=()):
    return _SD('DepthStencilState',
               children=[_SD(n, v) for n, v in pairs] + list(faces))


class TestD3DDepthAccess(unittest.TestCase):
    def test_none_state_is_unknown(self):
        self.assertIsNone(_d3d_depth_access(None))

    def test_all_disabled_is_access_none(self):
        self.assertEqual(_d3d_depth_access(_dss([])), ACCESS_NONE)

    def test_depth_test_read_only(self):
        dss = _dss([('DepthEnable', 1), ('DepthFunc', LESS),
                    ('DepthWriteMask', 0)])
        self.assertEqual(_d3d_depth_access(dss), READ)

    def test_depth_write_only(self):
        dss = _dss([('DepthEnable', 1), ('DepthFunc', ALWAYS),
                    ('DepthWriteMask', 1)])
        self.assertEqual(_d3d_depth_access(dss), WRITE)

    def test_depth_read_write(self):
        dss = _dss([('DepthEnable', 1), ('DepthFunc', LESS),
                    ('DepthWriteMask', 1)])
        self.assertEqual(_d3d_depth_access(dss), RW)

    def test_stencil_desc2_per_face_writemask(self):
        # DESC2: the face carries its own StencilWriteMask (top-level is 0)
        front = _SD('FrontFace', children=[
            _SD('StencilFunc', ALWAYS), _SD('StencilWriteMask', 0xFF),
            _SD('StencilFailOp', KEEP), _SD('StencilDepthFailOp', KEEP),
            _SD('StencilPassOp', REPLACE)])         # op != KEEP -> write
        dss = _dss([('DepthEnable', 0), ('StencilEnable', 1),
                    ('StencilWriteMask', 0)], faces=[front])
        self.assertEqual(_d3d_depth_access(dss), WRITE)

    def test_stencil_classic_top_level_writemask(self):
        # classic D3D11: the face has no mask, the top-level one supplies it
        front = _SD('FrontFace', children=[
            _SD('StencilFunc', LESS),               # != ALWAYS -> read
            _SD('StencilPassOp', REPLACE)])         # op != KEEP -> write
        dss = _dss([('DepthEnable', 0), ('StencilEnable', 1),
                    ('StencilWriteMask', 0xFF)], faces=[front])
        self.assertEqual(_d3d_depth_access(dss), RW)

    def test_stencil_masked_off_is_not_write(self):
        # write mask 0 means the op can't change the buffer -> no write
        front = _SD('FrontFace', children=[
            _SD('StencilFunc', ALWAYS), _SD('StencilWriteMask', 0),
            _SD('StencilPassOp', REPLACE)])
        dss = _dss([('DepthEnable', 0), ('StencilEnable', 1),
                    ('StencilWriteMask', 0)], faces=[front])
        self.assertEqual(_d3d_depth_access(dss), ACCESS_NONE)


if __name__ == '__main__':
    unittest.main()
