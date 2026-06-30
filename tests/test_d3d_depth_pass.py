# -*- coding: utf-8 -*-
"""D3D11 / D3D12 depth-pass chunk scanning tests."""
import unittest

from renderdoc_graph_viewer.parse import apis
from renderdoc_graph_viewer.parse.apis._common import READ, WRITE, RW, ACCESS_NONE

D3D11Pass = apis.depth_pass('d3d11')
D3D12Pass = apis.depth_pass('d3d12')

ALWAYS = 8   # D3D11/12_COMPARISON_FUNC_ALWAYS
LESS = 2


class _Node(object):
    """SDObject stand-in: a name, an optional int / ResourceId value, and named
    children that double as positional children (NumChildren / GetChild)."""

    def __init__(self, name, value=None, rid=None, children=None):
        self.name = name
        self._v = value
        self._rid = rid
        self._kids = list(children or [])

    def FindChild(self, name):
        for k in self._kids:
            if k.name == name:
                return k
        return None

    def NumChildren(self):
        return len(self._kids)

    def GetChild(self, i):
        return self._kids[i]

    def AsInt(self):
        return int(self._v)

    def AsResourceId(self):
        return self._rid


def _depth_fields(test=1, func=LESS, write_mask=0):
    """The depth-only fields _d3d_depth_access reads off a desc node."""
    return [_Node('DepthEnable', value=test),
            _Node('DepthFunc', value=func),
            _Node('DepthWriteMask', value=write_mask)]


# ------------------------------------------------------------------- D3D11

def _d3d11_create(state_id, dss_fields, named_out=True, desc_named=True):
    """An ID3D11Device::CreateDepthStencilState chunk. named_out=False drops the
    ppDepthStencilState child so the state id falls back to the last ResourceId;
    desc_named=False drops pDepthStencilDesc so the desc falls back to the child
    that carries DepthEnable."""
    desc = _Node('pDepthStencilDesc' if desc_named else 'desc',
                 children=dss_fields)
    kids = [desc]
    if named_out:
        kids.append(_Node('ppDepthStencilState', rid=state_id))
    else:
        kids.append(_Node('_trailing', rid=state_id))
    return _Node('ID3D11Device::CreateDepthStencilState', children=kids)


def _d3d11_bind(state_id):
    return _Node('ID3D11DeviceContext::OMSetDepthStencilState',
                 children=[_Node('pDepthStencilState', rid=state_id)])


class TestD3D11DepthPass(unittest.TestCase):
    def test_create_bind_current(self):
        chunks = [_d3d11_create('S_read', _depth_fields(write_mask=0)),
                  _d3d11_bind('S_read')]
        p = D3D11Pass(chunks)
        self.assertEqual(p.seen, 1)
        p.on_chunk(chunks[1])
        self.assertEqual(p.current(), READ)

    def test_unbound_defaults_to_rw(self):
        # nothing bound yet -> the D3D11 API default (test on, write all): RW
        p = D3D11Pass([_d3d11_create('S', _depth_fields())])
        self.assertEqual(p.current(), RW)

    def test_bind_switch_tracks_latest(self):
        chunks = [_d3d11_create('S_read', _depth_fields(write_mask=0)),
                  _d3d11_create('S_write',
                                _depth_fields(func=ALWAYS, write_mask=1)),
                  _d3d11_bind('S_read'), _d3d11_bind('S_write')]
        p = D3D11Pass(chunks)
        p.on_chunk(chunks[2])
        self.assertEqual(p.current(), READ)
        p.on_chunk(chunks[3])
        self.assertEqual(p.current(), WRITE)

    def test_desc_falls_back_to_child_with_depthenable(self):
        chunks = [_d3d11_create('S', _depth_fields(write_mask=1, func=ALWAYS),
                                desc_named=False),
                  _d3d11_bind('S')]
        p = D3D11Pass(chunks)
        p.on_chunk(chunks[1])
        self.assertEqual(p.current(), WRITE)

    def test_state_id_falls_back_to_last_resource_id(self):
        chunks = [_d3d11_create('S_rw', _depth_fields(write_mask=1),
                                named_out=False),
                  _d3d11_bind('S_rw')]
        p = D3D11Pass(chunks)
        p.on_chunk(chunks[1])
        self.assertEqual(p.current(), RW)


# ------------------------------------------------------------------- D3D12

def _d3d12_create(chunk_name, pso_id, dss_fields, pid_field='pPipelineState'):
    desc = _Node('pDesc', children=[
        _Node('DepthStencilState', children=dss_fields)])
    return _Node(chunk_name, children=[desc, _Node(pid_field, rid=pso_id)])


def _d3d12_bind(pso_id):
    return _Node('ID3D12GraphicsCommandList::SetPipelineState',
                 children=[_Node('pPipelineState', rid=pso_id)])


class TestD3D12DepthPass(unittest.TestCase):
    def test_classic_pso_create_bind_current(self):
        chunks = [_d3d12_create('ID3D12Device::CreateGraphicsPipelineState',
                                'P_rw', _depth_fields(func=LESS, write_mask=1)),
                  _d3d12_bind('P_rw')]
        p = D3D12Pass(chunks)
        self.assertEqual(p.seen, 1)
        p.on_chunk(chunks[1])
        self.assertEqual(p.current(), RW)

    def test_stream_pso_create_is_recognised(self):
        # CreatePipelineState is the stream-style form (modern RHIs / UE5);
        # pDesc.DepthStencilState carries the same fields as the classic call
        chunks = [_d3d12_create('ID3D12Device2::CreatePipelineState',
                                'P_read', _depth_fields(write_mask=0)),
                  _d3d12_bind('P_read')]
        p = D3D12Pass(chunks)
        self.assertEqual(p.seen, 1)
        p.on_chunk(chunks[1])
        self.assertEqual(p.current(), READ)

    def test_unbound_is_none(self):
        # D3D12 has no API default: an unbound pipeline keeps the conservative
        # write semantics upstream (current() is None, not a verdict)
        p = D3D12Pass([_d3d12_create(
            'ID3D12Device::CreateGraphicsPipelineState', 'P',
            _depth_fields())])
        self.assertIsNone(p.current())

    def test_state_id_from_alternate_pid_field(self):
        chunks = [_d3d12_create('ID3D12Device::CreateGraphicsPipelineState',
                                'P_none', _depth_fields(test=0, write_mask=0),
                                pid_field='PipelineState'),
                  _d3d12_bind('P_none')]
        p = D3D12Pass(chunks)
        p.on_chunk(chunks[1])
        self.assertEqual(p.current(), ACCESS_NONE)


if __name__ == '__main__':
    unittest.main()
