# -*- coding: utf-8 -*-
"""Static D3D12 shader-access reconstruction helper tests."""
import unittest

from renderdoc_graph_viewer.parse import apis
from renderdoc_graph_viewer.parse.apis import d3d12 as _d12

_RANGE_UAV = _d12._D3D12_RANGE_UAV     # 1
_APPEND = _d12._D3D12_APPEND           # 0xFFFFFFFF


class SDO(object):
    """Minimal fake SDObject: name + children + one scalar value. AsInt/AsString/
    AsResourceId raise on the wrong kind, as the real SDObject does, so the
    production accessors' defensive coercion is exercised too."""

    def __init__(self, name, kids=None, i=None, rid=None, s=None):
        self.name = name
        self._kids = kids or []
        self._i, self._r, self._s = i, rid, s

    def FindChild(self, n):
        for k in self._kids:
            if k.name == n:
                return k
        return None

    def NumChildren(self):
        return len(self._kids)

    def GetChild(self, idx):
        return self._kids[idx]

    def AsInt(self):
        if self._i is None:
            raise ValueError('not int')
        return self._i

    def AsResourceId(self):
        if self._r is None:
            raise ValueError('not rid')
        return self._r

    def AsString(self):
        if self._s is None:
            raise ValueError('not str')
        return self._s


# ----------------------------------------------------------- root signatures

def _range(range_type, base_reg, num, offset, space=0):
    return SDO('$range', [
        SDO('RangeType', i=range_type),
        SDO('BaseShaderRegister', i=base_reg),
        SDO('NumDescriptors', i=num),
        SDO('OffsetInDescriptorsFromTableStart', i=offset),
        SDO('RegisterSpace', i=space)])


def _rootsig(rsid, ranges):
    table = SDO('$param', [SDO('DescriptorTable',
                               [SDO('pDescriptorRanges', list(ranges))])])
    return SDO('ID3D12Device::CreateRootSignature', [
        SDO('pRootSignature', rid=rsid),
        SDO('UnpackedSignature', [SDO('Parameters', [table])])])


class TestRootSignatures(unittest.TestCase):
    def test_append_offset_accumulates_running_total(self):
        # range 0 at offset 0 spanning 2 descriptors, range 1 with APPEND -> 2
        chunk = _rootsig('RS', [
            _range(_RANGE_UAV, 0, 2, 0),
            _range(_RANGE_UAV, 2, 1, _APPEND)])
        out = _d12._d3d12_rootsigs([chunk])
        self.assertEqual(out['RS'], [[(_RANGE_UAV, 0, 2, 0, 0),
                                      (_RANGE_UAV, 2, 1, 2, 0)]])

    def test_explicit_offset_is_kept(self):
        chunk = _rootsig('RS', [_range(_RANGE_UAV, 4, 1, 7, space=1)])
        out = _d12._d3d12_rootsigs([chunk])
        self.assertEqual(out['RS'], [[(_RANGE_UAV, 4, 1, 7, 1)]])

    def test_param_without_descriptor_table_is_empty(self):
        # a root constant / root descriptor param carries no DescriptorTable
        chunk = SDO('CreateRootSignature', [
            SDO('pRootSignature', rid='RS'),
            SDO('UnpackedSignature', [SDO('Parameters', [SDO('$param', [])])])])
        out = _d12._d3d12_rootsigs([chunk])
        self.assertEqual(out['RS'], [[]])


# --------------------------------------------------------- heap initial state

def _desc(kind, index, resource):
    # positional layout the fast path validates: [type, heap, index, Resource]
    return SDO('$el', [SDO('type', s=kind), SDO('heap', rid='H'),
                       SDO('index', i=index), SDO('Resource', rid=resource)])


def _heap_initial(heap, descs):
    return SDO('Initial Contents', [
        SDO('type', s='Descriptor Heap'), SDO('id', rid=heap),
        SDO('Descriptors', list(descs))])


class TestHeapInitial(unittest.TestCase):
    def test_uav_slots_kept_srv_skipped(self):
        chunk = _heap_initial('H', [
            _desc('UAV', 5, 'BUF'),
            _desc('SRV', 6, 'TEX')])      # only UAV slots are resolved
        out = _d12._d3d12_heap_initial([chunk])
        self.assertEqual(out, {('H', 5): 'BUF'})

    def test_non_descriptor_heap_initial_ignored(self):
        chunk = SDO('Initial Contents', [
            SDO('type', s='Buffer'), SDO('id', rid='H'),
            SDO('Descriptors', [_desc('UAV', 0, 'BUF')])])
        self.assertEqual(_d12._d3d12_heap_initial([chunk]), {})


# --------------------------------------------------------------- PSO links

class _Res(object):
    def __init__(self, rid, rtype, parents=()):
        self.resourceId = rid
        self.type = rtype
        self.parentResources = list(parents)


class _Ctl(object):
    def __init__(self, resources=()):
        self._res = list(resources)

    def GetResources(self):
        return self._res


class TestPsoLinks(unittest.TestCase):
    def test_pso_links_to_shader_and_root_signature(self):
        ctl = _Ctl([
            _Res('CS', 'ResourceType.Shader'),
            _Res('RS', 'ResourceType.ShaderBinding'),
            _Res('PSO', 'ResourceType.PipelineState', parents=['CS', 'RS'])])
        out = _d12._d3d12_pso_links(ctl)
        self.assertEqual(out, {'PSO': ('CS', 'RS')})

    def test_pso_without_shader_parent_is_omitted(self):
        # a graphics PSO (no compute shader resource) yields no compute link
        ctl = _Ctl([
            _Res('RS', 'ResourceType.ShaderBinding'),
            _Res('PSO', 'ResourceType.PipelineState', parents=['RS'])])
        self.assertEqual(_d12._d3d12_pso_links(ctl), {})


# ------------------------------------------------ CopyDescriptors resolution

def _create_uav(heap, index, resource):
    return SDO('ID3D12Device::CreateUnorderedAccessView', [
        SDO('desc', [SDO('Resource', rid=resource)]),
        SDO('dst', [SDO('heap', rid=heap), SDO('index', i=index)])])


def _copy(dst_heap, dst_idx, src_heap, src_idx):
    el = SDO('$copy', [
        SDO('dst', [SDO('heap', rid=dst_heap), SDO('index', i=dst_idx)]),
        SDO('src', [SDO('heap', rid=src_heap), SDO('index', i=src_idx)])])
    return SDO('ID3D12Device::CopyDescriptors', [SDO('DescriptorCopies', [el])])


class TestCopyResolution(unittest.TestCase):
    def _pass(self, chunks):
        return apis.refine_pass('d3d12')(_Ctl([]), None, chunks)

    def test_resolve_follows_copy_to_created_view(self):
        # a UAV created in heap SH:5, then copied into the shader-visible DH:2
        p = self._pass([_create_uav('SH', 5, 'BUF'),
                        _copy('DH', 2, 'SH', 5)])
        self.assertEqual(p._resolve('DH', 2), 'BUF')
        self.assertEqual(p._resolve('SH', 5), 'BUF')   # the direct slot too

    def test_resolve_unmapped_slot_is_none(self):
        p = self._pass([_create_uav('SH', 5, 'BUF')])
        self.assertIsNone(p._resolve('DH', 99))


if __name__ == '__main__':
    unittest.main()
