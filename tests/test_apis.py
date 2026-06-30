# -*- coding: utf-8 -*-
"""Per-API registry and shared helpers (parse.apis / _common): depth/refine/
present lookups for known + unknown APIs, the access-vocabulary constants, the
_merge and combine_depth_access folds, api_key derivation, and the D3D12
Present-source resolver."""
import types
import unittest

from renderdoc_graph_viewer.parse import apis
from renderdoc_graph_viewer.parse.apis import _common
from renderdoc_graph_viewer.parse.apis import d3d12


class TestApiRegistry(unittest.TestCase):
    def test_unknown_api_returns_none(self):
        self.assertIsNone(apis.depth_pass('no_such_api'))
        self.assertIsNone(apis.refine_pass('no_such_api'))
        self.assertIsNone(apis.present_resolver('no_such_api'))

    def test_access_vocabulary_values(self):
        self.assertEqual(_common.READ, 'read')
        self.assertEqual(_common.WRITE, 'write')
        self.assertEqual(_common.RW, 'rw')
        self.assertEqual(_common.ACCESS_NONE, 'none')
        self.assertEqual(_common.UNUSED, 'unused')

    def test_merge_lattice(self):
        m = _common._merge
        self.assertEqual(m(None, _common.READ), _common.READ)
        self.assertEqual(m(_common.READ, _common.WRITE), _common.RW)
        self.assertEqual(m(_common.UNUSED, _common.READ), _common.READ)

    def test_combine_depth_access_folds_flags(self):
        c = _common.combine_depth_access
        self.assertEqual(c(False, False), _common.ACCESS_NONE)
        self.assertEqual(c(True, False), _common.READ)
        self.assertEqual(c(False, True), _common.WRITE)
        self.assertEqual(c(True, True), _common.RW)


class TestApiKey(unittest.TestCase):
    @staticmethod
    def _ctl(pipeline_type):
        return types.SimpleNamespace(
            GetAPIProperties=lambda: types.SimpleNamespace(
                pipelineType=pipeline_type))

    def test_strips_enum_namespace_and_lowercases(self):
        # rd.GraphicsAPI.* stringifies as 'GraphicsAPI.<Name>'
        self.assertEqual(apis.api_key(self._ctl('GraphicsAPI.D3D12')), 'd3d12')
        self.assertEqual(apis.api_key(self._ctl('GraphicsAPI.Vulkan')), 'vulkan')

    def test_bare_value_without_namespace(self):
        self.assertEqual(apis.api_key(self._ctl('OpenGL')), 'opengl')

    def test_propagates_controller_failure(self):
        # controller failures propagate
        def boom():
            raise RuntimeError('no API')
        ctl = types.SimpleNamespace(GetAPIProperties=boom)
        with self.assertRaises(RuntimeError):
            apis.api_key(ctl)


class _Obj(object):
    def __init__(self, name='', value=None, children=None):
        self.name = name
        self.value = value
        self.children = children or {}

    def AsResourceId(self):
        return self.value

    def FindChild(self, name):
        return self.children.get(name)

    def NumChildren(self):
        if isinstance(self.children, list):
            return len(self.children)
        return 0

    def GetChild(self, index):
        return self.children[index]


class _Chunk(_Obj):
    pass


def _rid(value):
    return _Obj(value=value)


def _array(*children):
    return _Obj(children=list(children))


def _rtv(resource):
    return _Obj(children={'Resource': _rid(resource)})


class _Event(object):
    def __init__(self, chunk_index):
        self.chunkIndex = chunk_index


class _Action(object):
    def __init__(self, eid, chunk_index):
        self.eventId = eid
        self.events = [_Event(chunk_index)]


class TestD3D12PresentResolver(unittest.TestCase):
    def _resolver(self, chunks):
        return d3d12.PRESENT_RESOLVER(chunks)

    def test_presented_image_chunk_wins_when_non_null(self):
        chunks = [
            _Chunk('IDXGISwapChain::GetBuffer',
                   children={'SwapbufferID': _rid('ResourceId::19932')}),
            _Chunk('Internal::End of Capture',
                   children={'PresentedImage': _rid('ResourceId::19932')}),
        ]
        r = self._resolver(chunks)
        self.assertEqual(r.resolve(_Action(20, 1)), 'ResourceId::19932')

    def test_null_presented_image_uses_drawn_swapbuffer_rtv(self):
        chunks = [
            _Chunk('IDXGISwapChain::GetBuffer',
                   children={'SwapbufferID': _rid('ResourceId::19932')}),
            _Chunk('IDXGISwapChain::GetBuffer',
                   children={'SwapbufferID': _rid('ResourceId::19933')}),
            _Chunk('ID3D12GraphicsCommandList::OMSetRenderTargets',
                   children={
                       'pCommandList': _rid('ResourceId::CL'),
                       'pRenderTargetDescriptors': _array(
                           _rtv('ResourceId::19933')),
                   }),
            _Chunk('ID3D12GraphicsCommandList::DrawInstanced',
                   children={'pCommandList': _rid('ResourceId::CL')}),
            _Chunk('Internal::End of Capture',
                   children={'PresentedImage': _rid('ResourceId::0')}),
        ]
        r = self._resolver(chunks)
        self.assertEqual(r.resolve(_Action(20, 4)), 'ResourceId::19933')

    def test_null_presented_image_uses_last_unique_rtv(self):
        chunks = [
            _Chunk('ID3D12GraphicsCommandList::OMSetRenderTargets',
                   children={
                       'pCommandList': _rid('ResourceId::CL'),
                       'pRenderTargetDescriptors': _array(
                           _rtv('ResourceId::1121')),
                   }),
            _Chunk('ID3D12GraphicsCommandList::DrawInstanced',
                   children={'pCommandList': _rid('ResourceId::CL')}),
            _Chunk('Internal::End of Capture',
                   children={'PresentedImage': _rid('ResourceId::0')}),
        ]
        r = self._resolver(chunks)
        self.assertEqual(r.resolve(_Action(20, 2)), 'ResourceId::1121')

    def test_null_presented_image_ignores_later_rtv_writes(self):
        chunks = [
            _Chunk('ID3D12GraphicsCommandList::OMSetRenderTargets',
                   children={
                       'pCommandList': _rid('ResourceId::CL'),
                       'pRenderTargetDescriptors': _array(
                           _rtv('ResourceId::A')),
                   }),
            _Chunk('ID3D12GraphicsCommandList::DrawInstanced',
                   children={'pCommandList': _rid('ResourceId::CL')}),
            _Chunk('Internal::End of Capture',
                   children={'PresentedImage': _rid('ResourceId::0')}),
            _Chunk('ID3D12GraphicsCommandList::OMSetRenderTargets',
                   children={
                       'pCommandList': _rid('ResourceId::CL'),
                       'pRenderTargetDescriptors': _array(
                           _rtv('ResourceId::B')),
                   }),
            _Chunk('ID3D12GraphicsCommandList::DrawInstanced',
                   children={'pCommandList': _rid('ResourceId::CL')}),
        ]
        r = self._resolver(chunks)
        self.assertEqual(r.resolve(_Action(20, 2)), 'ResourceId::A')

    def test_null_presented_image_does_not_guess_multiple_rtvs(self):
        chunks = [
            _Chunk('IDXGISwapChain::GetBuffer',
                   children={'SwapbufferID': _rid('ResourceId::19932')}),
            _Chunk('IDXGISwapChain::GetBuffer',
                   children={'SwapbufferID': _rid('ResourceId::19933')}),
            _Chunk('ID3D12GraphicsCommandList::OMSetRenderTargets',
                   children={
                       'pCommandList': _rid('ResourceId::CL'),
                       'pRenderTargetDescriptors': _array(
                           _rtv('ResourceId::19932'),
                           _rtv('ResourceId::19933')),
                   }),
            _Chunk('ID3D12GraphicsCommandList::DrawInstanced',
                   children={'pCommandList': _rid('ResourceId::CL')}),
            _Chunk('Internal::End of Capture',
                   children={'PresentedImage': _rid('ResourceId::0')}),
        ]
        r = self._resolver(chunks)
        self.assertIsNone(r.resolve(_Action(20, 4)))


if __name__ == '__main__':
    unittest.main()
