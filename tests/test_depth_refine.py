# -*- coding: utf-8 -*-
"""Depth-access refinement: classify depth-attachment
bindings from the pipeline's BAKED depth/stencil state, parsed
statically out of the structured capture data (zero replay)."""
import types
import unittest

from tests import rd_stub
rd = rd_stub.install()

from renderdoc_graph_viewer.parse import depth_access
from renderdoc_graph_viewer.parse import usage_access as ua
from renderdoc_graph_viewer.parse import usage_cleanup
from renderdoc_graph_viewer.graph_model import extract_bundle

ALWAYS = 7   # VK_COMPARE_OP_ALWAYS
LESS = 1
KEEP = 0     # VK_STENCIL_OP_KEEP
REPLACE = 2


class _SD(object):
    """Minimal SDObject stand-in: name + int value or children."""

    def __init__(self, name, value=None, children=None, rid=None):
        self.name = name
        self._v = value
        self._rid = rid
        self._kids = list(children or [])

    def NumChildren(self):
        return len(self._kids)

    def GetChild(self, i):
        return self._kids[i]

    def FindChild(self, name):
        for k in self._kids:
            if k.name == name:
                return k
        return None

    def AsInt(self):
        return int(self._v)

    def AsResourceId(self):
        return self._rid


def _face(name, cmp_op=ALWAYS, fail=KEEP, passop=KEEP, dfail=KEEP,
          mask=255):
    return _SD(name, children=[
        _SD('failOp', fail), _SD('passOp', passop),
        _SD('depthFailOp', dfail), _SD('compareOp', cmp_op),
        _SD('writeMask', mask)])


def _dss(test=0, write=0, cmp_op=LESS, stest=0, front=None, back=None,
         null=False):
    if null:
        return _SD('pDepthStencilState', children=[])
    return _SD('pDepthStencilState', children=[
        _SD('depthTestEnable', test), _SD('depthWriteEnable', write),
        _SD('depthCompareOp', cmp_op), _SD('stencilTestEnable', stest),
        front or _face('front'), back or _face('back')])


def _create(pid, dss):
    return _SD('vkCreateGraphicsPipelines', children=[
        _SD('Pipeline', rid=pid),
        _SD('CreateInfo', children=[dss])])


def _bind(pid, point=0):
    return _SD('vkCmdBindPipeline', children=[
        _SD('pipelineBindPoint', point), _SD('pipeline', rid=pid)])


def _act(eid, chunk_idx_list, children=None, flags=0):
    return types.SimpleNamespace(
        eventId=eid, flags=flags,
        events=[types.SimpleNamespace(chunkIndex=i, eventId=eid)
                for i in chunk_idx_list],
        children=children or [])


class _Ctl(object):
    def __init__(self, chunks, roots, api=None):
        self.chunks = chunks
        self.roots = roots
        self.api = api or rd.GraphicsAPI.Vulkan

    def GetAPIProperties(self):
        return types.SimpleNamespace(pipelineType=self.api)

    def GetStructuredFile(self):
        return types.SimpleNamespace(chunks=self.chunks)

    def GetRootActions(self):
        return self.roots


def _depth_refine_payload(ctl, ubr, leaf_eids, progress=None, warnings=None):
    leaves = [types.SimpleNamespace(eid=eid) for eid in leaf_eids]
    result = depth_access.refine(
        ctl, rd, ubr, leaves, progress=progress, warnings=warnings)
    return result.get('access') or {}, set()


def _label_cleanup(ctl):
    result = usage_cleanup.collect_label_cleanup(ctl, rd)
    return set(result.get('labels') or ())


def _refine(ctl, draw_eids):
    ubr = {'A': [(e, 'DepthStencilTarget') for e in draw_eids]}
    acc, _labels = _depth_refine_payload(ctl, ubr, set(draw_eids))
    return acc


class TestRefine(unittest.TestCase):
    def test_classification_four_ways(self):
        chunks = [
            _create('P_read', _dss(test=1, write=0)),
            _create('P_write', _dss(test=1, write=1, cmp_op=ALWAYS)),
            _create('P_rw', _dss(test=1, write=1)),
            _create('P_none', _dss(test=0, write=0)),
            _bind('P_read'), _bind('P_write'), _bind('P_rw'),
            _bind('P_none'),
        ]
        roots = [_act(10, [4]), _act(11, [11]),   # draw chunk idx unused
                 _act(20, [5]), _act(21, [11]),
                 _act(30, [6]), _act(31, [11]),
                 _act(40, [7]), _act(41, [11])]
        # give every draw its own preceding bind via events
        roots = [_act(10, [4]), _act(20, [5]), _act(30, [6]),
                 _act(40, [7])]
        ctl = _Ctl(chunks + [_SD('vkCmdDraw')], roots)
        acc = _refine(ctl, [10, 20, 30, 40])
        self.assertEqual(acc, {10: 'read', 20: 'write', 30: 'rw',
                               40: 'none'})

    def test_null_depth_state_is_none(self):
        chunks = [_create('P', _dss(null=True)), _bind('P')]
        ctl = _Ctl(chunks, [_act(5, [1])])
        self.assertEqual(_refine(ctl, [5]), {5: 'none'})

    def test_stencil_write_and_read(self):
        chunks = [
            _create('P_sw', _dss(stest=1,
                                 front=_face('front', passop=REPLACE))),
            _create('P_sr', _dss(stest=1,
                                 front=_face('front', cmp_op=LESS))),
            _create('P_sm', _dss(stest=1,
                                 front=_face('front', passop=REPLACE,
                                             mask=0))),
            _bind('P_sw'), _bind('P_sr'), _bind('P_sm'),
        ]
        roots = [_act(1, [3]), _act(2, [4]), _act(3, [5])]
        ctl = _Ctl(chunks, roots)
        acc = _refine(ctl, [1, 2, 3])
        self.assertEqual(acc, {1: 'write', 2: 'read', 3: 'none'})

    def test_bind_carries_across_draws_and_switches(self):
        chunks = [_create('A', _dss(test=1, write=0)),
                  _create('B', _dss(test=1, write=1)),
                  _bind('A'), _bind('B')]
        # draw 1/2 under A, then switch, draw 3 under B
        roots = [_act(1, [2]), _act(2, []), _act(3, [3])]
        ctl = _Ctl(chunks, roots)
        acc = _refine(ctl, [1, 2, 3])
        self.assertEqual(acc, {1: 'read', 2: 'read', 3: 'rw'})

    def test_compute_bind_does_not_clobber_graphics(self):
        chunks = [_create('G', _dss(test=1, write=0)),
                  _bind('G'), _bind('CS', point=1)]
        roots = [_act(1, [1]), _act(2, [2])]   # CS bind between draws
        ctl = _Ctl(chunks, roots)
        acc = _refine(ctl, [1, 2])
        self.assertEqual(acc, {1: 'read', 2: 'read'})

    def test_unknown_pipeline_falls_back(self):
        chunks = [_bind('NeverCreated')]
        ctl = _Ctl(chunks, [_act(1, [0])])
        self.assertEqual(_refine(ctl, [1]), {})

    def test_nested_actions_walked_in_order(self):
        chunks = [_create('A', _dss(test=1, write=0)), _bind('A')]
        marker = _act(0, [1], children=[_act(7, [])])
        ctl = _Ctl(chunks, [marker])
        self.assertEqual(_refine(ctl, [7]), {7: 'read'})

    def test_non_leaf_event_not_classified(self):
        chunks = [_create('A', _dss(test=1, write=0)), _bind('A')]
        ctl = _Ctl(chunks, [_act(1, [1]), _act(2, [])])
        ubr = {'A': [(1, 'DepthStencilTarget'),
                     (2, 'DepthStencilTarget')]}
        acc, _labels = _depth_refine_payload(ctl, ubr, {1})
        self.assertEqual(acc, {1: 'read'})  # 2 not a leaf

    def test_non_vulkan_returns_empty(self):
        ctl = _Ctl([], [], api=rd.GraphicsAPI.D3D11)
        self.assertEqual(_refine(ctl, [1]), {})

    def test_label_actions_collected_by_marker_flags(self):
        # API-agnostic: a purely marker-class action executes
        # nothing - its eid joins the strip set
        chunks = [_create('A', _dss(test=1, write=0)), _bind('A')]
        roots = [_act(1, [1]),
                 _act(2, [], flags=rd.ActionFlags.PopMarker)]
        ctl = _Ctl(chunks, roots)
        ubr = {'A': [(1, 'DepthStencilTarget'),
                     (2, 'DepthStencilTarget')]}
        acc, _labels = _depth_refine_payload(ctl, ubr, {1})
        labels = _label_cleanup(ctl)
        self.assertEqual(acc, {1: 'read'})
        self.assertEqual(labels, {2})

    def test_structural_marker_not_a_label(self):
        # vkCmdExecuteCommands-style: marker flag + structural flag
        # executes real work - never stripped
        roots = [_act(2, [], flags=(rd.ActionFlags.PopMarker |
                                    rd.ActionFlags.CmdList))]
        ctl = _Ctl([], roots)
        acc, _labels = _depth_refine_payload(
            ctl, {'A': [(2, 'DepthStencilTarget')]}, set())
        labels = _label_cleanup(ctl)
        self.assertEqual(labels, set())

    def test_labels_collected_even_off_vulkan(self):
        # the phantom strip is API-agnostic; only depth classification
        # is Vulkan-gated
        roots = [_act(5, [], flags=rd.ActionFlags.PushMarker)]
        ctl = _Ctl([], roots, api=rd.GraphicsAPI.D3D11)
        acc, _labels = _depth_refine_payload(
            ctl, {'A': [(5, 'ColorTarget')]}, set())
        labels = _label_cleanup(ctl)
        self.assertEqual(acc, {})
        self.assertEqual(labels, {5})

    def test_non_label_boundary_event_keeps_legacy_write(self):
        chunks = [_create('A', _dss(test=1, write=0)), _bind('A'),
                  _SD('vkCmdEndRenderPass')]
        roots = [_act(1, [1]), _act(2, [2])]
        ctl = _Ctl(chunks, roots)
        ubr = {'A': [(1, 'DepthStencilTarget'),
                     (2, 'DepthStencilTarget')]}
        acc, _labels = _depth_refine_payload(ctl, ubr, {1})
        labels = _label_cleanup(ctl)
        self.assertEqual(acc, {1: 'read'})
        self.assertEqual(labels, set())


class TestStripLabelUsages(unittest.TestCase):
    def test_strips_every_usage_kind_on_label_events(self):
        # colour phantoms forge writers exactly like the depth ones did
        ubr = {'RT': [(5, 'ColorTarget'), (9, 'ColorTarget')],
               'D': [(5, 'DepthStencilTarget'), (7, 'PS_Resource')]}
        usage_cleanup.strip_label_usages(ubr, frozenset({5}))
        self.assertEqual(ubr['RT'], [(9, 'ColorTarget')])
        self.assertEqual(ubr['D'], [(7, 'PS_Resource')])

    def test_empty_labels_noop(self):
        ubr = {'RT': [(5, 'ColorTarget')]}
        usage_cleanup.strip_label_usages(ubr, frozenset())
        self.assertEqual(ubr['RT'], [(5, 'ColorTarget')])


def _d3d_dss(enable=1, write_mask=1, func=2, stencil=0):
    face = _SD('FrontFace', children=[
        _SD('StencilFailOp', 1), _SD('StencilDepthFailOp', 1),
        _SD('StencilPassOp', 1), _SD('StencilFunc', 8)])
    return _SD('DepthStencilState', children=[
        _SD('DepthEnable', enable), _SD('DepthWriteMask', write_mask),
        _SD('DepthFunc', func), _SD('StencilEnable', stencil),
        _SD('StencilWriteMask', 255), face,
        _SD('BackFace', children=[])])


class TestD3D12Adapter(unittest.TestCase):
    def _create(self, pid, **kw):
        return _SD('ID3D12Device::CreateGraphicsPipelineState',
                   children=[_SD('pDesc', children=[_d3d_dss(**kw)]),
                             _SD('pPipelineState', rid=pid)])

    def _bind(self, pid):
        return _SD('ID3D12GraphicsCommandList::SetPipelineState',
                   children=[_SD('pPipelineState', rid=pid)])

    def _create_stream(self, pid, **kw):
        # ID3D12Device2::CreatePipelineState - RenderDoc expands the
        # stream desc, so pDesc.DepthStencilState looks identical to the
        # classic descriptor (UE5 / modern RHIs emit only this form)
        return _SD('ID3D12Device2::CreatePipelineState',
                   children=[_SD('pDesc', children=[_d3d_dss(**kw)]),
                             _SD('pPipelineState', rid=pid)])

    def test_classification(self):
        # D3D enums: COMPARISON_ALWAYS=8, LESS=2, WRITE_MASK ZERO/ALL
        chunks = [
            self._create('R', write_mask=0),            # test-only
            self._create('W', write_mask=1, func=8),    # pure write
            self._create('RW', write_mask=1),           # read-write
            self._create('N', enable=0),                # inert
            self._bind('R'), self._bind('W'), self._bind('RW'),
            self._bind('N'),
        ]
        roots = [_act(1, [4]), _act(2, [5]), _act(3, [6]), _act(4, [7])]
        ctl = _Ctl(chunks, roots, api=rd.GraphicsAPI.D3D12)
        ubr = {'A': [(e, 'DepthStencilTarget') for e in (1, 2, 3, 4)]}
        acc, _l = _depth_refine_payload(ctl, ubr, {1, 2, 3, 4})
        self.assertEqual(acc, {1: 'read', 2: 'write', 3: 'rw',
                               4: 'none'})

    def test_stream_pso_classified(self):
        # the stock filter only matched CreateGraphicsPipelineState and
        # silently skipped every UE5 CreatePipelineState (a UE5 capture)
        chunks = [
            self._create_stream('R', write_mask=0),         # test-only
            self._create_stream('W', write_mask=1, func=8), # pure write
            self._bind('R'), self._bind('W'),
        ]
        roots = [_act(1, [2]), _act(2, [3])]
        ctl = _Ctl(chunks, roots, api=rd.GraphicsAPI.D3D12)
        ubr = {'A': [(1, 'DepthStencilTarget'),
                     (2, 'DepthStencilTarget')]}
        acc, _l = _depth_refine_payload(ctl, ubr, {1, 2})
        self.assertEqual(acc, {1: 'read', 2: 'write'})

    def test_desc2_per_face_stencil_write_mask(self):
        # per-face stencil write mask
        def _full_face(name, passop, func=8):
            return _SD(name, children=[
                _SD('StencilFailOp', 1), _SD('StencilDepthFailOp', 1),
                _SD('StencilPassOp', passop), _SD('StencilFunc', func),
                _SD('StencilWriteMask', 255)])
        dss = _SD('DepthStencilState', children=[
            _SD('DepthEnable', 0), _SD('DepthWriteMask', 1),
            _SD('DepthFunc', 2), _SD('StencilEnable', 1),
            _full_face('FrontFace', passop=2),   # REPLACE -> writes
            _full_face('BackFace', passop=1)])   # KEEP -> inert
        create = _SD('ID3D12Device2::CreatePipelineState',
                     children=[_SD('pDesc', children=[dss]),
                               _SD('pPipelineState', rid='S')])
        ctl = _Ctl([create, self._bind('S')], [_act(1, [1])],
                   api=rd.GraphicsAPI.D3D12)
        acc, _l = _depth_refine_payload(
            ctl, {'A': [(1, 'DepthStencilTarget')]}, {1})
        self.assertEqual(acc, {1: 'write'})

    def test_parse_failure_warns_and_disables(self):
        broken = _SD('ID3D12Device::CreateGraphicsPipelineState',
                     children=[_SD('UnexpectedLayout')])
        ctl = _Ctl([broken, self._bind('X')],
                   [_act(1, [1])], api=rd.GraphicsAPI.D3D12)
        warns = []
        acc, _l = _depth_refine_payload(
            ctl, {'A': [(1, 'DepthStencilTarget')]}, {1},
            warnings=warns)
        self.assertEqual(acc, {})
        self.assertTrue(any('d3d12' in w for w in warns))


class TestD3D11Adapter(unittest.TestCase):
    def _create(self, pid, **kw):
        d = _d3d_dss(**kw)
        d.name = 'pDepthStencilDesc'
        return _SD('ID3D11Device::CreateDepthStencilState',
                   children=[d, _SD('ppDepthStencilState', rid=pid)])

    def _bind(self, pid):
        return _SD('ID3D11DeviceContext::OMSetDepthStencilState',
                   children=[_SD('pDepthStencilState', rid=pid),
                             _SD('StencilRef', 0)])

    def test_bound_state_object(self):
        chunks = [self._create('S', write_mask=0), self._bind('S')]
        ctl = _Ctl(chunks, [_act(1, [1])], api=rd.GraphicsAPI.D3D11)
        acc, _l = _depth_refine_payload(
            ctl, {'A': [(1, 'DepthStencilTarget')]}, {1})
        self.assertEqual(acc, {1: 'read'})

    def test_unbound_and_null_mean_api_default_rw(self):
        chunks = [self._create('S', write_mask=0),
                  self._bind('ResourceId::0')]
        # draw 1 unbound, draw 2 after binding NULL
        roots = [_act(1, []), _act(2, [1])]
        ctl = _Ctl(chunks, roots, api=rd.GraphicsAPI.D3D11)
        ubr = {'A': [(1, 'DepthStencilTarget'),
                     (2, 'DepthStencilTarget')]}
        acc, _l = _depth_refine_payload(ctl, ubr, {1, 2})
        self.assertEqual(acc, {1: 'rw', 2: 'rw'})


class TestUnsupportedApi(unittest.TestCase):
    GL_DEPTH_TEST = 0x0B71
    GL_ALWAYS = 0x0207

    def _chunks(self):
        return [
            _SD('glEnable', children=[_SD('cap', self.GL_DEPTH_TEST)]),
            _SD('glDepthMask', children=[_SD('flag', 0)]),
            _SD('glDepthFunc', children=[_SD('func', self.GL_ALWAYS)]),
            _SD('glDepthMask', children=[_SD('flag', 1)]),
        ]

    def test_unsupported_api_falls_back_to_no_refinement(self):
        # The registry has no depth pass for OpenGL, so an OpenGL capture
        # refines nothing and every depth binding keeps its conservative write
        # semantics (empty access map).
        roots = [_act(1, []), _act(2, [0]), _act(3, [1]),
                 _act(4, [2, 3])]
        ctl = _Ctl(self._chunks(), roots, api=rd.GraphicsAPI.OpenGL)
        ubr = {'A': [(e, 'DepthStencilTarget') for e in (1, 2, 3, 4)]}
        acc, _l = _depth_refine_payload(ctl, ubr, {1, 2, 3, 4})
        self.assertEqual(acc, {})


class TestApply(unittest.TestCase):
    def test_usage_names_registered(self):
        self.assertEqual(ua.USAGE_ACCESS['DepthTestRead'], ua.READ)
        self.assertEqual(ua.USAGE_ACCESS['DepthTestRW'], ua.RW)

    def test_rewrite_and_drop(self):
        ubr = {'A': [(1, 'DepthStencilTarget'), (2, 'DepthStencilTarget'),
                     (3, 'DepthStencilTarget'), (4, 'DepthStencilTarget'),
                     (5, 'PS_Resource')]}
        ua.apply_depth_access(ubr, {1: 'read', 2: 'rw', 3: 'none',
                                    4: 'write'})
        self.assertEqual(ubr['A'], [(1, 'DepthTestRead'),
                                    (2, 'DepthTestRW'),
                                    (4, 'DepthStencilTarget'),
                                    (5, 'PS_Resource')])

    def test_unrefined_events_untouched(self):
        ubr = {'A': [(7, 'DepthStencilTarget')]}
        ua.apply_depth_access(ubr, {1: 'read'})
        self.assertEqual(ubr['A'], [(7, 'DepthStencilTarget')])

    def test_empty_access_noop(self):
        ubr = {'A': [(7, 'DepthStencilTarget')]}
        ua.apply_depth_access(ubr, {})
        self.assertEqual(ubr['A'], [(7, 'DepthStencilTarget')])


class TestExtractCachePath(unittest.TestCase):
    def test_cached_access_skips_walk_and_applies(self):
        # cached depth access path
        from tests.test_candidates import _Controller, _Tex, TC
        ctl = _Controller([_Tex('T_depth', TC.DepthTarget)], [])
        cached = {'access': {10: 'read'}}
        bundle = extract_bundle(ctl, refinement_cache={'depth_access': cached})
        self.assertEqual(bundle['refinement_cache']['depth_access'], cached)

    def test_cached_labels_strip_usages(self):
        from tests.test_candidates import _Controller

        class _C(_Controller):
            def GetUsage(self, rid):
                import types
                return [types.SimpleNamespace(eventId=99, usage='x')]

        # direct strip over usage_by_res
        ubr = {'T': [(99, 'ColorTarget'), (100, 'ColorTarget')]}
        usage_cleanup.strip_label_usages(ubr, frozenset([99]))
        self.assertEqual(ubr['T'], [(100, 'ColorTarget')])


if __name__ == '__main__':
    unittest.main()
