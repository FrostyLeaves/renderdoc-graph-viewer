# -*- coding: utf-8 -*-
"""Offline tests for the YAML render-graph e2e harness."""
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, '..'))
_GPU = os.path.join(_REPO, 'tests', 'e2e')
for _p in (_REPO, _GPU):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from gen import schema as S
from gen import hlsl_codegen as hc
from gen import manifest as mf
import refmodel as rm
import compare

SCENES = os.path.join(_GPU, 'scenes')


def _cs(binds):
    p = S.Pass('P', S.PASS_COMPUTE, binds=[S.Binding(*b) for b in binds])
    return hc.gen_compute(p)


class TestSchema(unittest.TestCase):
    def test_loads_compute_chain(self):
        sc = S.load_scene(os.path.join(SCENES, 'compute_chain.yaml'))
        self.assertEqual(sc.name, 'compute_chain')
        self.assertEqual(len(sc.resources), 6)
        self.assertEqual([p.name for p in sc.passes],
                         ['Generate', 'Process', 'Reduce'])
        self.assertEqual(sc.resources['Image'].kind, S.KIND_UAV_TEX)

    def test_rejects_write_on_srv(self):
        with self.assertRaises(S.SchemaError):
            S.load_dict({'resources': {'A': {'kind': 'buffer'}},
                         'passes': [{'name': 'P', 'type': 'compute',
                                     'bind': [{'res': 'A', 'bind': 'srv_buf',
                                               'access': 'write'}]}]})

    def test_rejects_undeclared_resource(self):
        with self.assertRaises(S.SchemaError):
            S.load_dict({'resources': {},
                         'passes': [{'name': 'P', 'type': 'compute',
                                     'bind': [{'res': 'Ghost', 'bind': 'uav_buf',
                                               'access': 'rw'}]}]})

    def test_cbv_requires_cbuffer_resource(self):
        """A cbv slot bound to a plain buffer (not a cbuffer) is rejected."""
        with self.assertRaises(S.SchemaError):
            S.load_dict({'resources': {'P': {'kind': 'buffer'},
                                       'O': {'kind': 'buffer'}},
                         'passes': [{'name': 'A', 'type': 'compute',
                                     'bind': [{'res': 'P', 'bind': 'cbv',
                                               'access': 'read'},
                                              {'res': 'O', 'bind': 'uav_buf',
                                               'access': 'write'}]}]})

    def test_cbuffer_only_bound_as_cbv(self):
        """A cbuffer resource bound through a non-cbv slot is rejected."""
        with self.assertRaises(S.SchemaError):
            S.load_dict({'resources': {'P': {'kind': 'cbuffer'}},
                         'passes': [{'name': 'A', 'type': 'compute',
                                     'bind': [{'res': 'P', 'bind': 'uav_buf',
                                               'access': 'write'}]}]})

    def test_vertex_must_be_vbuffer(self):
        with self.assertRaises(S.SchemaError):
            S.load_dict({'resources': {'V': {'kind': 'buffer'},
                                       'C': {'kind': 'color'}},
                         'passes': [{'name': 'M', 'type': 'graphics',
                                     'color': ['C'], 'vertex': ['V']}]})

    def test_index_must_be_ibuffer(self):
        with self.assertRaises(S.SchemaError):
            S.load_dict({'resources': {'V': {'kind': 'vbuffer'},
                                       'I': {'kind': 'buffer'},
                                       'C': {'kind': 'color'}},
                         'passes': [{'name': 'M', 'type': 'graphics',
                                     'color': ['C'],
                                     'vertex': ['V'], 'index': 'I'}]})

    def test_ia_buffer_not_shader_bound(self):
        with self.assertRaises(S.SchemaError):
            S.load_dict({'resources': {'V': {'kind': 'vbuffer'}},
                         'passes': [{'name': 'P', 'type': 'compute',
                                     'bind': [{'res': 'V', 'bind': 'srv_buf',
                                               'access': 'read'}]}]})

    def test_frame_prior_flag(self):
        """frame_prior keeps the same graph shape."""
        sc = S.load_scene(os.path.join(SCENES, 'frame_prior.yaml'))
        self.assertTrue(sc.frame_prior)
        _fp_nodes, fp_edges = rm.expected(sc)
        # same accesses as an in-frame scene would produce: read/write/rw edges
        self.assertIn(
            'edge|res|buffer|Input|1|pass|compute|Generate|read|0', fp_edges)
        self.assertIn(
            'edge|pass|compute|Generate|res|buffer|Work|1|write|0', fp_edges)


class TestCodegen(unittest.TestCase):
    """The access column controls exactly which load/store appears."""

    def test_write_only_has_no_load(self):
        src = _cs([('W', S.BIND_UAV_BUF, S.ACC_WRITE)])
        self.assertIn('g0[i] =', src)
        self.assertNotIn('+= g0[i]', src)   # no read of g0
        self.assertNotIn('g0[i] +', src)

    def test_read_only_has_no_store(self):
        src = _cs([('R', S.BIND_UAV_BUF, S.ACC_READ),
                  ('W', S.BIND_UAV_BUF, S.ACC_WRITE)])
        # g0 is read-only: appears as a read, never on a store LHS
        self.assertIn('acc += g0[i];', src)
        self.assertNotIn('g0[i] =', src)

    def test_none_uses_getdimensions_only(self):
        src = _cs([('N', S.BIND_UAV_BUF, S.ACC_NONE),
                  ('W', S.BIND_UAV_BUF, S.ACC_WRITE)])
        self.assertIn('g0.GetDimensions', src)
        self.assertNotIn('g0[i]', src)  # neither load nor store via subscript

    def test_rw_has_both(self):
        src = _cs([('X', S.BIND_UAV_BUF, S.ACC_RW)])
        self.assertIn('g0[i] = g0[i]', src)  # read and store

    def test_atomic_emits_interlocked(self):
        p = S.Pass('P', S.PASS_COMPUTE,
                   binds=[S.Binding('X', S.BIND_UAV_BUF, S.ACC_RW, atomic=True)])
        src = hc.gen_compute(p)
        self.assertIn('InterlockedAdd', src)
        self.assertNotIn('X[i] = X[i]', src)

    def test_cbv_emits_cbuffer_and_reads_member(self):
        # cbv (g0, register b0) read into acc, then consumed by the uav write (g1)
        src = _cs([('P', S.BIND_CBV, S.ACC_READ),
                  ('O', S.BIND_UAV_BUF, S.ACC_WRITE)])
        self.assertIn('cbuffer', src)
        self.assertIn('register(b0)', src)
        self.assertIn('acc += g0_cv;', src)     # the constant is read
        self.assertNotIn('g0_cv =', src)        # ... and never written (CBV is read-only)
        self.assertIn('g1[i] =', src)           # the uav write that consumes it

    def test_vertex_graphics_reads_each_stream(self):
        p = S.Pass('Mesh', S.PASS_GRAPHICS, color=['Albedo'],
                   vertex=['Stream0', 'Stream1', 'Stream2'], index='Indices')
        vs, ps = hc.gen_graphics(p)
        # one float4 attribute per stream, each summed into SV_Position so the IA
        # genuinely consumes every vertex buffer
        self.assertIn('ATTR0', vs)
        self.assertIn('ATTR1', vs)
        self.assertIn('ATTR2', vs)
        self.assertIn('VLOC(2)', vs)
        self.assertIn('v.a0 + v.a1 + v.a2', vs)
        self.assertIn('SV_Target0', ps)


class TestManifest(unittest.TestCase):
    """Canonical register/binding layout."""

    def test_layout_registers(self):
        sc = S.load_scene(os.path.join(SCENES, 'compute_chain.yaml'))
        m = mf.emit_manifest(sc)
        proc = [p for p in m['passes'] if p['name'] == 'Process'][0]
        regs = [(b['res'], b['hlsl_reg'], b['vk_binding']) for b in proc['binds']]
        self.assertEqual(regs, [('Work', 'u0', 0), ('Accum', 'u1', 1),
                                ('Out', 'u2', 2), ('Image', 'u3', 3)])
        gen = [p for p in m['passes'] if p['name'] == 'Generate'][0]
        # SRV and UAV are numbered per register class
        self.assertEqual([(b['res'], b['hlsl_reg']) for b in gen['binds']],
                         [('Input', 't0'), ('Work', 'u0')])

    def test_cbv_layout(self):
        sc = S.load_scene(os.path.join(SCENES, 'cbv.yaml'))
        m = mf.emit_manifest(sc)
        ap = [p for p in m['passes'] if p['name'] == 'Apply'][0]
        pb = [b for b in ap['binds'] if b['res'] == 'Params'][0]
        # cbv lands in the 'b' register class as a Vulkan uniform buffer
        self.assertEqual((pb['reg_class'], pb['hlsl_reg'], pb['vk_dtype']),
                         ('b', 'b0', 'uniform_buffer'))
        # the runtime gets the distinct cbuffer kind so it creates a constant buffer
        pr = [r for r in m['resources'] if r['name'] == 'Params'][0]
        self.assertEqual(pr['kind'], 'cbuffer')

    def test_vertex_index(self):
        sc = S.load_scene(os.path.join(SCENES, 'vertex_index.yaml'))
        m = mf.emit_manifest(sc)
        mesh = [p for p in m['passes'] if p['name'] == 'Mesh'][0]
        self.assertEqual(mesh['vertex'], ['Stream0', 'Stream1', 'Stream2'])
        self.assertEqual(mesh['index'], 'Indices')


class TestOracle(unittest.TestCase):
    """Synthetic e2e graph model tests."""

    def test_refined_matches_golden_semantics(self):
        sc = S.load_scene(os.path.join(SCENES, 'compute_chain.yaml'))
        nodes, edges = rm.expected(sc)
        # write-only / read-only become single clean edges, all version 1
        self.assertIn('edge|pass|compute|Generate|res|buffer|Work|1|write|0', edges)
        self.assertIn('edge|res|buffer|Work|1|pass|compute|Process|read|0', edges)
        # rw: write edge present, self-read folded away (single writer)
        self.assertIn('edge|pass|compute|Process|res|buffer|Accum|1|write|0', edges)
        self.assertIn('edge|pass|compute|Reduce|res|buffer|Result|1|write|0', edges)
        # no spurious read of a write-only target
        self.assertNotIn('edge|res|buffer|Out|1|pass|compute|Generate|read|0', edges)
        # every resource is single-version under refinement
        self.assertTrue(all('|2' not in n for n in nodes if n.startswith('res|')))

    def test_unused_dashes_read_only(self):
        """Bound-but-unused UAV read edge is dashed."""
        sc = S.load_scene(os.path.join(SCENES, 'unused.yaml'))
        _, ref = rm.expected(sc)
        self.assertIn('edge|res|buffer|A|1|pass|compute|Probe|read|1', ref)  # dashed

    def test_models_synthetic_present(self):
        sc = S.load_scene(os.path.join(SCENES, 'compute_chain.yaml'))
        nodes, _ = rm.expected(sc)
        self.assertIn('pass|present|Present', nodes)

    def test_cbv_read_edge(self):
        """A constant buffer is a RES_BUFFER node read by the compute pass via
        CS_Constants; CBVs are not shader-refined -- only UAVs are."""
        sc = S.load_scene(os.path.join(SCENES, 'cbv.yaml'))
        nodes, edges = rm.expected(sc)
        self.assertIn('res|buffer|Params|1', nodes)
        self.assertIn('edge|res|buffer|Params|1|pass|compute|Apply|read|0', edges)
        self.assertIn('edge|pass|compute|Apply|res|buffer|Out|1|write|0', edges)

    def test_vertex_index_streams_bundle_index_separate(self):
        """With bundling, the 3 equivalent vertex streams collapse to one node
        while the index buffer stays separate; both feed the draw with IA edges."""
        sc = S.load_scene(os.path.join(SCENES, 'vertex_index.yaml'))
        nodes, edges = rm.expected(sc, bundling=sc.bundling)
        self.assertIn('resbundle|buffer|Stream0,Stream1,Stream2|1', nodes)
        self.assertNotIn('res|buffer|Stream0|1', nodes)      # merged into the bundle
        self.assertIn('res|buffer|Indices|1', nodes)         # index stays separate
        self.assertIn('edge|resbundle|buffer|Stream0,Stream1,Stream2|1'
                      '|pass|graphics|Mesh|read|0', edges)
        self.assertIn('edge|res|buffer|Indices|1|pass|graphics|Mesh|read|0', edges)
        self.assertIn('edge|pass|graphics|Mesh|res|color|Albedo|1|write|0', edges)

    def test_vertex_index_merged_keeps_streams_separate(self):
        """The merged whole-frame view does not bundle, so every stream is its
        own node."""
        sc = S.load_scene(os.path.join(SCENES, 'vertex_index.yaml'))
        nodes, _ = rm.expected_merged(sc)
        for s in ('Stream0', 'Stream1', 'Stream2', 'Indices'):
            self.assertIn('res|buffer|%s|1' % s, nodes)
        self.assertFalse(any(n.startswith('resbundle|') for n in nodes))

    def test_dual_role_distinct_side_instances(self):
        """Repeated Side marker enumerates as two instances."""
        sc = S.load_scene(os.path.join(SCENES, 'dual_role.yaml'))
        insts = {k: (p, r) for k, p, r in rm.scope_instances(sc)}
        self.assertIn('Side#0', insts)
        self.assertIn('Side#1', insts)
        s0 = rm.expected_instance(sc, *insts['Side#0'])[0]
        s1 = rm.expected_instance(sc, *insts['Side#1'])[0]
        self.assertIn('pass|compute|Prep', s0)
        self.assertIn('pass|compute|Seed', s0)
        self.assertIn('pass|compute|Collect', s1)
        self.assertIn('pass|compute|Finish', s1)
        self.assertNotEqual(s0, s1)

    def test_dual_role_frame_portal_split(self):
        """Focusing Frame splits the dual-role Side scope into a producer portal
        (it feeds Shared into Frame) and a consumer portal (reads Frame's Output)."""
        sc = S.load_scene(os.path.join(SCENES, 'dual_role.yaml'))
        insts = {k: (p, r) for k, p, r in rm.scope_instances(sc)}
        nodes, _ = rm.expected_instance(sc, *insts['Frame#0'])
        self.assertIn('portal|producer|Side', nodes)
        self.assertIn('portal|consumer|Side', nodes)

    def test_markerless_fine_groups(self):
        """A markerless pass drops its name segment -> build_passes fine-groups it
        (Compute #N), so no marker-named pass node appears."""
        sc = S.load_scene(os.path.join(SCENES, 'markerless.yaml'))
        nodes, _ = rm.expected(sc)
        self.assertTrue(any(n.startswith('pass|compute|Compute') for n in nodes))
        self.assertFalse(any('GenA' in n or 'GenB' in n for n in nodes))

    def test_merged_is_single_version_per_resource(self):
        """Merged whole-frame view: one node per resource (no version splitting),
        full-path pass grouping, present node."""
        sc = S.load_scene(os.path.join(SCENES, 'compute_chain.yaml'))
        nodes, _edges = rm.expected_merged(sc)
        self.assertIn('pass|present|Present', nodes)
        # every resource is a single merged node (vN with N>=2 only appears versioned)
        self.assertTrue(all('|2' not in n for n in nodes if n.startswith('res|')))
        self.assertIn('res|buffer|Work|1', nodes)


class _Leaf(object):
    def __init__(self, eid, marker_path):
        self.eid = eid
        self.marker_path = marker_path


class TestInstanceKey(unittest.TestCase):
    def test_disambiguates_repeated_markers(self):
        """Two contiguous runs of the same marker get distinct ordinals; a path
        string alone would collide them."""
        leaves = [_Leaf(1, ('Side', 'Prep')), _Leaf(2, ('Side', 'Seed')),
                  _Leaf(3, ('Frame', 'InnerA')), _Leaf(4, ('Frame', 'InnerB')),
                  _Leaf(5, ('Side', 'Collect')), _Leaf(6, ('Side', 'Finish'))]
        self.assertEqual(compare.instance_key(leaves, (), None), '')
        self.assertEqual(compare.instance_key(leaves, ('Side',), (1, 2)), 'Side#0')
        self.assertEqual(compare.instance_key(leaves, ('Side',), (5, 6)), 'Side#1')
        self.assertEqual(compare.instance_key(leaves, ('Frame',), (3, 4)), 'Frame#0')


if __name__ == '__main__':
    unittest.main()
