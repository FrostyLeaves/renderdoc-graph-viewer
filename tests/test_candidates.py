# -*- coding: utf-8 -*-
"""Resource-candidate admission rules (config panel)."""
import unittest

from tests import rd_stub
rd = rd_stub.install()

from renderdoc_graph_viewer import config
from renderdoc_graph_viewer.graph_model import (texture_kind_of,
                                             buffer_admitted,
                                             extract_bundle)

TC = rd.TextureCategory
BC = rd.BufferCategory


def _cands(**over):
    c = config.candidates_of(config.DEFAULTS)
    c.update(over)
    return c


# the conservative ruleset (classic four texture classes,
# RW/Indirect buffers): rule-gating tests use it explicitly so they
# don't depend on what the factory DEFAULTS table happens to enable
_CLASSIC = {
    'tex_color': True, 'tex_depth': True, 'tex_rw': True,
    'tex_swap': True, 'tex_other': False,
    'buf_rw': True, 'buf_indirect': True,
    'buf_vertex_index': False, 'buf_constants': False,
    'buf_noflags': False,
}


def _classic(**over):
    c = dict(_CLASSIC)
    c.update(over)
    return c


class TestTextureAdmission(unittest.TestCase):
    def test_classic_kinds_with_defaults(self):
        self.assertEqual(texture_kind_of(TC.ColorTarget, TC), 'color')
        self.assertEqual(texture_kind_of(TC.DepthTarget, TC), 'depth')
        self.assertEqual(texture_kind_of(TC.ShaderReadWrite, TC), 'uav_tex')
        self.assertEqual(texture_kind_of(TC.SwapBuffer, TC), 'swapchain')

    def test_priority_swap_depth_color_rw(self):
        self.assertEqual(
            texture_kind_of(TC.DepthTarget | TC.ColorTarget, TC), 'depth')
        self.assertEqual(
            texture_kind_of(TC.SwapBuffer | TC.ColorTarget, TC), 'swapchain')
        self.assertEqual(
            texture_kind_of(TC.ColorTarget | TC.ShaderReadWrite, TC), 'color')

    def test_factory_defaults_admit_everything(self):
        # the DEFAULTS table admits every class
        self.assertEqual(texture_kind_of(TC.ShaderRead, TC), 'sampled')
        self.assertEqual(texture_kind_of(TC.NoFlags, TC), 'sampled')

    def test_other_textures_excluded_when_switched_off(self):
        c = _classic()
        self.assertIsNone(texture_kind_of(TC.ShaderRead, TC, c))
        self.assertIsNone(texture_kind_of(TC.NoFlags, TC, c))

    def test_tex_other_admits_sampled_assets(self):
        c = _classic(tex_other=True)
        self.assertEqual(texture_kind_of(TC.ShaderRead, TC, c), 'sampled')
        self.assertEqual(texture_kind_of(TC.NoFlags, TC, c), 'sampled')

    def test_tex_other_does_not_admit_disabled_classic_class(self):
        # ColorTarget texture with color class disabled
        c = _cands(tex_color=False, tex_other=True)
        self.assertIsNone(texture_kind_of(TC.ColorTarget, TC, c))

    def test_disabled_priority_class_is_not_admitted_by_lower_flag(self):
        # ColorTarget|ShaderReadWrite classifies as color, so only the color
        # switch controls admission.
        c = _cands(tex_color=False)
        self.assertIsNone(
            texture_kind_of(TC.ColorTarget | TC.ShaderReadWrite, TC, c))

    def test_swapchain_switch_excludes_swapbuffer_color_target(self):
        c = _cands(tex_swap=False)
        self.assertIsNone(
            texture_kind_of(TC.SwapBuffer | TC.ColorTarget, TC, c))

    def test_each_class_switch_excludes(self):
        self.assertIsNone(
            texture_kind_of(TC.DepthTarget, TC,
                            _classic(tex_depth=False)))
        self.assertIsNone(
            texture_kind_of(TC.ShaderReadWrite, TC,
                            _classic(tex_rw=False)))
        self.assertIsNone(
            texture_kind_of(TC.SwapBuffer, TC, _classic(tex_swap=False)))


class TestBufferAdmission(unittest.TestCase):
    def test_factory_defaults_admit_everything(self):
        for cf in (BC.ReadWrite, BC.Indirect, BC.Vertex, BC.Index,
                   BC.Constants, BC.NoFlags):
            self.assertTrue(buffer_admitted(cf, BC), cf)

    def test_classic_ruleset_rw_indirect_only(self):
        c = _classic()
        self.assertTrue(buffer_admitted(BC.ReadWrite, BC, c))
        self.assertTrue(buffer_admitted(BC.Indirect, BC, c))
        self.assertFalse(buffer_admitted(BC.Vertex, BC, c))
        self.assertFalse(buffer_admitted(BC.Index, BC, c))
        self.assertFalse(buffer_admitted(BC.Constants, BC, c))
        self.assertFalse(buffer_admitted(BC.NoFlags, BC, c))

    def test_vertex_index_switch(self):
        c = _classic(buf_vertex_index=True)
        self.assertTrue(buffer_admitted(BC.Vertex, BC, c))
        self.assertTrue(buffer_admitted(BC.Index, BC, c))
        self.assertFalse(buffer_admitted(BC.Constants, BC, c))

    def test_constants_switch(self):
        c = _classic(buf_constants=True)
        self.assertTrue(buffer_admitted(BC.Constants, BC, c))

    def test_noflags_switch_admits_only_flagless(self):
        # the Buffer 212215 case: copy destination with creationFlags==0
        c = _classic(buf_noflags=True)
        self.assertTrue(buffer_admitted(BC.NoFlags, BC, c))
        self.assertFalse(buffer_admitted(BC.Vertex, BC, c))

    def test_all_categories_off_excludes_everything(self):
        # no master switch: clearing every buffer category is how
        # the panel excludes all buffers
        c = _cands(buf_rw=False, buf_indirect=False, buf_vertex_index=False,
                   buf_constants=False, buf_noflags=False)
        for cf in (BC.ReadWrite, BC.Indirect, BC.Vertex, BC.Constants,
                   BC.NoFlags):
            self.assertFalse(buffer_admitted(cf, BC, c))

    def test_category_switches_off(self):
        self.assertFalse(
            buffer_admitted(BC.ReadWrite, BC, _cands(buf_rw=False)))
        self.assertFalse(
            buffer_admitted(BC.Indirect, BC, _cands(buf_indirect=False)))


# --------------------------------------------------- extract integration

class _Fmt(object):
    def Name(self):
        return 'RGBA8'


class _Tex(object):
    def __init__(self, key, flags):
        self.resourceId = rd.ResourceId(key)
        self.creationFlags = flags
        self.width, self.height = 64, 64
        self.format = _Fmt()
        self.msSamp = 1


class _Buf(object):
    def __init__(self, key, flags):
        self.resourceId = rd.ResourceId(key)
        self.creationFlags = flags
        self.length = 256


class _Controller(object):
    def __init__(self, textures, buffers):
        self._t, self._b = textures, buffers

    def GetResources(self):
        return []

    def GetTextures(self):
        return self._t

    def GetBuffers(self):
        return self._b

    def GetUsage(self, rid):
        return []

    def GetStructuredFile(self):
        return None

    def GetRootActions(self):
        return []


class TestExtractBundleCandidates(unittest.TestCase):
    def _make(self):
        texs = [_Tex('T_color', TC.ColorTarget),
                _Tex('T_asset', TC.ShaderRead)]
        bufs = [_Buf('B_rw', BC.ReadWrite),
                _Buf('B_vtx', BC.Vertex),
                _Buf('B_raw', BC.NoFlags)]
        return _Controller(texs, bufs)

    def test_defaults_admit_everything(self):
        b = extract_bundle(self._make())
        self.assertEqual(set(b['res_info']),
                         {'T_color', 'T_asset', 'B_rw', 'B_vtx', 'B_raw'})
        self.assertEqual(b['res_info']['T_color']['kind'], 'color')
        self.assertEqual(b['res_info']['T_asset']['kind'], 'sampled')
        self.assertEqual(b['res_info']['B_raw']['kind'], 'buffer')

    def test_include_buffers_false_still_works(self):
        b = extract_bundle(self._make(), include_buffers=False)
        self.assertEqual(set(b['res_info']), {'T_color', 'T_asset'})

    def test_classic_ruleset_narrows_admission(self):
        b = extract_bundle(self._make(), candidates=_classic())
        self.assertEqual(set(b['res_info']), {'T_color', 'B_rw'})

    def test_candidates_narrow_to_nothing(self):
        c = _cands(tex_color=False, tex_other=False, buf_rw=False,
                   buf_indirect=False, buf_vertex_index=False,
                   buf_constants=False, buf_noflags=False)
        b = extract_bundle(self._make(), candidates=c)
        self.assertEqual(set(b['res_info']), set())


if __name__ == '__main__':
    unittest.main()
