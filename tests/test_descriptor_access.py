# -*- coding: utf-8 -*-
"""Unit tests for the static Vulkan descriptor reconstruction core
(apis.vulkan._vk_build_set_contents / _vk_pso_shaders), using fake
structured-data chunks. The full controller-driven path (GetShader + parse) is
validated end-to-end against a real capture by tools/verify_vk_refine.py."""
import unittest

from renderdoc_graph_viewer.parse.apis._common import READ, WRITE, RW, UNUSED, _merge
from renderdoc_graph_viewer.parse.apis import vulkan as _vk


class SDO(object):
    """Minimal fake SDObject: name + children + one scalar value."""
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


def _layout(lid, bindings):
    # bindings: [(binding, count)]
    els = [SDO('$el', [SDO('binding', i=b), SDO('descriptorCount', i=c)])
           for (b, c) in bindings]
    return SDO('vkCreateDescriptorSetLayout',
               [SDO('CreateInfo', [SDO('pBindings', els)]),
                SDO('SetLayout', rid=lid)])


def _alloc(setid, lid):
    return SDO('vkAllocateDescriptorSets',
               [SDO('AllocateInfo', [SDO('pSetLayouts', [SDO('$el', rid=lid)])]),
                SDO('DescriptorSet', rid=setid)])


def _imgview(view, image):
    return SDO('vkCreateImageView',
               [SDO('CreateInfo', [SDO('image', rid=image)]),
                SDO('View', rid=view)])


def _initial(setid, slot_resources):
    binds = [SDO('$el', [SDO('resource', rid=r)]) for r in slot_resources]
    return SDO('Initial Contents',
               [SDO('type', s='eResDescriptorSet'), SDO('id', rid=setid),
                SDO('Bindings', binds)])


def _tmpl_update(setid, dst_binding, buffers, dst_elem=None, texel=None):
    """One vkUpdateDescriptorSetWithTemplate write: dst_binding + a list of
    buffer (or texel-buffer-view) resource ids, modelling a write whose
    descriptorCount spans consecutive bindings."""
    fields = [SDO('dstBinding', i=dst_binding)]
    if dst_elem is not None:
        fields.append(SDO('dstArrayElement', i=dst_elem))
    if texel is not None:
        fields.append(SDO('pTexelBufferView', [SDO('$el', rid=r) for r in texel]))
    else:
        fields.append(SDO('pBufferInfo',
                          [SDO('$el', [SDO('buffer', rid=r)]) for r in buffers]))
    return SDO('vkUpdateDescriptorSetWithTemplate',
               [SDO('descriptorSet', rid=setid),
                SDO('Decoded Writes', [SDO('$el', fields)])])


class TestInitialContents(unittest.TestCase):
    def test_flat_to_binding_and_view_resolve(self):
        # layout: binding 0 (count 1) then binding 1 (count 1); flat slot order matches
        chunks = [
            _layout('L1', [(0, 1), (1, 1)]),
            _alloc('S1', 'L1'),
            _imgview('V1', 'IMG1'),                 # view -> underlying image
            _initial('S1', ['V1', 'BUF1']),         # slot0 = imageView, slot1 = buffer
        ]
        sb = _vk._vk_build_set_contents(chunks)
        self.assertEqual(sb['S1'][0], {'IMG1'})   # binding 0: imageView V1 resolved to IMG1
        self.assertEqual(sb['S1'][1], {'BUF1'})   # binding 1: buffer kept as-is

    def test_arrayed_binding_offset(self):
        # binding 0 has count 2 (array), binding 5 has count 1 -> flat [b0e0, b0e1, b5e0]
        chunks = [
            _layout('L2', [(0, 2), (5, 1)]),
            _alloc('S2', 'L2'),
            _initial('S2', ['BA', 'BB', 'BC']),
        ]
        sb = _vk._vk_build_set_contents(chunks)
        self.assertEqual(sb['S2'][0], {'BA', 'BB'})   # binding 0 array elements
        self.assertEqual(sb['S2'][5], {'BC'})         # binding 5 after the 2-element binding


class TestTemplateUpdate(unittest.TestCase):
    def test_overlays_binding(self):
        # in-frame template update writes binding 0 of S3 with image view V9 -> IMG9
        writes = SDO('Decoded Writes', [
            SDO('$el', [SDO('dstBinding', i=0),
                        SDO('pImageInfo', [SDO('$el', [SDO('imageView', rid='V9')])])])])
        chunks = [
            _layout('L3', [(0, 1)]),
            _alloc('S3', 'L3'),
            _imgview('V9', 'IMG9'),
            _initial('S3', ['BUF0']),
            SDO('vkUpdateDescriptorSetWithTemplate',
                [SDO('descriptorSet', rid='S3'), writes]),
        ]
        sb = _vk._vk_build_set_contents(chunks)
        # both the initial buffer and the template-written image are present
        self.assertEqual(sb['S3'][0], {'BUF0', 'IMG9'})

    def test_spills_across_consecutive_bindings(self):
        # one write, dstBinding=0, 4 buffers -> bindings 0,1,2,3
        chunks = [
            _layout('L', [(0, 1), (1, 1), (2, 1), (3, 1)]),
            _alloc('S', 'L'),
            _tmpl_update('S', 0, ['B0', 'B1', 'B2', 'B3']),
        ]
        sb = _vk._vk_build_set_contents(chunks)
        self.assertEqual(sb['S'][0], {'B0'})
        self.assertEqual(sb['S'][1], {'B1'})
        self.assertEqual(sb['S'][2], {'B2'})
        self.assertEqual(sb['S'][3], {'B3'})

    def test_arrayed_binding_stays_one_binding(self):
        # binding 0 has count 3 -> a 3-buffer write fills only binding 0
        chunks = [
            _layout('L', [(0, 3), (1, 1)]),
            _alloc('S', 'L'),
            _tmpl_update('S', 0, ['A', 'B', 'C']),
        ]
        sb = _vk._vk_build_set_contents(chunks)
        self.assertEqual(sb['S'][0], {'A', 'B', 'C'})
        self.assertNotIn(1, sb['S'])

    def test_dst_array_element_offset(self):
        # binding 0 count 4, dstArrayElement=2 -> two buffers fill the rest of it
        chunks = [
            _layout('L', [(0, 4), (1, 1)]),
            _alloc('S', 'L'),
            _tmpl_update('S', 0, ['A', 'B'], dst_elem=2),
        ]
        sb = _vk._vk_build_set_contents(chunks)
        self.assertEqual(sb['S'][0], {'A', 'B'})
        self.assertNotIn(1, sb['S'])

    def test_texel_buffer_view(self):
        chunks = [
            _layout('L', [(0, 1), (1, 1)]),
            _alloc('S', 'L'),
            _tmpl_update('S', 0, None, texel=['TBV0', 'TBV1']),
        ]
        sb = _vk._vk_build_set_contents(chunks)
        self.assertEqual(sb['S'][0], {'TBV0'})
        self.assertEqual(sb['S'][1], {'TBV1'})

    def test_no_layout_omits_spill(self):
        # no layout known: extras are omitted
        chunks = [_tmpl_update('S', 5, ['A', 'B', 'C'])]   # no _layout/_alloc for S
        sb = _vk._vk_build_set_contents(chunks)
        self.assertEqual(sb['S'][5], {'A'})
        self.assertEqual(sum(len(v) for v in sb['S'].values()), 1)   # extras omitted, not collapsed


class TestMerge(unittest.TestCase):
    def test_unused_yields_to_real_access(self):
        # real access wins over unused for the same (eid, resource)
        self.assertEqual(_merge(None, UNUSED), UNUSED)
        self.assertEqual(_merge(UNUSED, UNUSED), UNUSED)
        self.assertEqual(_merge(UNUSED, READ), READ)
        self.assertEqual(_merge(WRITE, UNUSED), WRITE)
        self.assertEqual(_merge(UNUSED, RW), RW)
        # genuine read + write still escalates to rw
        self.assertEqual(_merge(READ, WRITE), RW)


class TestPsoShaders(unittest.TestCase):
    def test_compute_and_graphics(self):
        chunks = [
            SDO('vkCreateComputePipelines', [
                SDO('CreateInfo', [SDO('stage', [SDO('module', rid='CSMOD'),
                                                 SDO('pName', s='main')])]),
                SDO('Pipeline', rid='CPSO')]),
            SDO('vkCreateGraphicsPipelines', [
                SDO('CreateInfo', [SDO('pStages', [
                    SDO('$el', [SDO('stage', s='VK_SHADER_STAGE_VERTEX_BIT'),
                                SDO('module', rid='VSMOD'), SDO('pName', s='main')]),
                    SDO('$el', [SDO('stage', s='VK_SHADER_STAGE_FRAGMENT_BIT'),
                                SDO('module', rid='PSMOD'), SDO('pName', s='main')])])]),
                SDO('Pipeline', rid='GPSO')]),
        ]
        psos = _vk._vk_pso_shaders(chunks)
        self.assertIn('Compute', psos['CPSO'])
        self.assertEqual(str(psos['CPSO']['Compute'][0]), 'CSMOD')
        self.assertEqual(set(psos['GPSO'].keys()), {'Vertex', 'Pixel'})
        self.assertEqual(str(psos['GPSO']['Pixel'][0]), 'PSMOD')


if __name__ == '__main__':
    unittest.main()
