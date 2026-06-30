# -*- coding: utf-8 -*-
"""Shader-access edge effects on the graph: unused-binding dashing (dash a read
edge only when all its shader-usage events are confirmed unused) and the usage
rename/de-edging that turns refined RW bindings into clean single edges."""

import unittest

from renderdoc_graph_viewer.graph_model import build_passes, build_graph
from renderdoc_graph_viewer import graph_model as gm
from renderdoc_graph_viewer.parse import usage_access as ua
from tests import fakes
from tests.fakes import draw

RES_INFO = {
    'A': {'kind': 'color', 'info': {}},
    'B': {'kind': 'color', 'info': {}},
}


def _fg():
    passes = build_passes([
        draw(10, ('A',), markers=('P1',)),
        draw(20, ('B',), markers=('P2',)),
        draw(30, ('B',), markers=('P3',)),
    ])
    return build_graph(passes, {
        'A': [(10, 'ColorTarget'), (20, 'PS_Resource'), (30, 'PS_Resource')],
        'B': [(20, 'ColorTarget'), (30, 'CopySrc')],
    }, RES_INFO, versioned=True)


def _baked_shader_bundle(bundle, shader_access):
    ua.apply_shader_access(bundle['usage_by_res'], shader_access)
    bundle['refinements'] = {'shader_access': dict(shader_access)}
    return bundle


class TestApply(unittest.TestCase):
    def _read_edge(self, fg, res_key):
        ids = [n.id for n in fg.resources if n.res_key == res_key]
        return [e for e in fg.edges
                if e.kind == 'read' and e.src_id in ids]

    def test_all_unused_goes_dashed(self):
        fg = _fg()
        ua.apply_unused_binding_flags(
            fg, {(20, 'A'): 'unused', (30, 'A'): 'unused'})
        self.assertTrue(all(e.unused_binding
                            for e in self._read_edge(fg, 'A')))

    def test_any_used_event_stays_solid(self):
        fg = _fg()
        ua.apply_unused_binding_flags(
            fg, {(20, 'A'): 'unused', (30, 'A'): 'rw'})
        edges = self._read_edge(fg, 'A')
        by_eid = {}
        for e in edges:
            for eid, _u in e.usages:
                by_eid[eid] = e
        self.assertTrue(by_eid[20].unused_binding)
        self.assertFalse(by_eid[30].unused_binding)

    def test_pending_results_stay_solid(self):
        fg = _fg()
        ua.apply_unused_binding_flags(
            fg, {(20, 'A'): 'unused'})  # (30,'A') unknown
        edges = self._read_edge(fg, 'A')
        for e in edges:
            eids = [eid for eid, _u in e.usages]
            if eids == [30]:
                self.assertFalse(e.unused_binding)

    def test_fixed_function_reads_never_dash(self):
        fg = _fg()
        ua.apply_unused_binding_flags(fg, {})
        self.assertTrue(all(not e.unused_binding
                            for e in self._read_edge(fg, 'B')))


class TestShaderAccessApply(unittest.TestCase):
    def test_refined_rw_usage_names_registered(self):
        self.assertEqual(ua.USAGE_ACCESS['PS_ReadResource'], ua.READ)
        self.assertEqual(ua.USAGE_ACCESS['CS_WriteResource'], ua.WRITE)
        self.assertEqual(ua.USAGE_ACCESS['CS_RWResource'], ua.RW)   # 原始名仍是 RW

    def test_apply_shader_access_renames_for_dedge(self):
        # bufC: PS 只读 -> PS_RWResource 改 PS_ReadResource;bufB: CS 只写 -> CS_WriteResource
        usage = {
            'bufC': [(10, 'PS_RWResource')],
            'bufB': [(11, 'CS_RWResource')],
            'bufD': [(12, 'CS_RWResource')],   # rw -> 不变
            'bufE': [(13, 'CS_RWResource')],   # unused -> 不变(虚线另走)
        }
        access = {(10, 'bufC'): 'read', (11, 'bufB'): 'write',
                  (12, 'bufD'): 'rw', (13, 'bufE'): 'unused'}
        ua.apply_shader_access(usage, access)
        self.assertEqual(usage['bufC'], [(10, 'PS_ReadResource')])
        self.assertEqual(usage['bufB'], [(11, 'CS_WriteResource')])
        self.assertEqual(usage['bufD'], [(12, 'CS_RWResource')])
        self.assertEqual(usage['bufE'], [(13, 'CS_RWResource')])

    def test_apply_access_rename_drop_removes_event(self):
        usage = {'d': [(5, 'DepthStencilTarget'), (6, 'PS_Resource')]}
        ua.apply_access_rename(
            usage, lookup=lambda eid, rk: ua.ACCESS_NONE if eid == 5 else None,
            usage_filter=lambda u: u == 'DepthStencilTarget',
            rename={ua.ACCESS_NONE: ua.DROP})
        self.assertEqual(usage['d'], [(6, 'PS_Resource')])   # eid 5 dropped

    def test_build_scoped_shader_access_drops_edges(self):
        # 一个 CS pass 对 bufW 只写、bufR 只读;精化后 bufW 无入边、bufR 无出边
        bundle = fakes.compute_bundle(writes_rw=['bufW', 'bufR'])
        sa_map = {(fakes.CS_EID, 'bufW'): 'write', (fakes.CS_EID, 'bufR'): 'read'}
        fg = gm.build_scoped(_baked_shader_bundle(bundle, sa_map), (), None)
        edges = [(e.src_id, e.dst_id, e.kind) for e in fg.edges]
        # bufW: 只有 write 出边(pass->res),没有 read 入边(res->pass)
        self.assertTrue(any(e[2] == gm.WRITE for e in edges if 'bufW' in (e[0] + e[1])))
        self.assertFalse(any(e[2] == gm.READ for e in edges if 'bufW' in (e[0] + e[1])))
        # bufR: 只有 read 入边,没有 write 出边
        self.assertTrue(any(e[2] == gm.READ for e in edges if 'bufR' in (e[0] + e[1])))
        self.assertFalse(any(e[2] == gm.WRITE for e in edges if 'bufR' in (e[0] + e[1])))

    def test_rebuild_seam_read_only_dedges_and_solid(self):
        bundle = fakes.compute_bundle(writes_rw=['bufR'])
        sa = {(fakes.CS_EID, 'bufR'): 'read'}
        fg = gm.build_scoped(_baked_shader_bundle(bundle, sa), (), None)
        edges = [(e.src_id, e.dst_id, e.kind, e.unused_binding) for e in fg.edges]
        bufR_edges = [e for e in edges if 'bufR' in (e[0] + e[1])]
        # read-only: a read (incoming) edge exists, no write (outgoing) edge
        self.assertTrue(any(e[2] == gm.READ for e in bufR_edges))
        self.assertFalse(any(e[2] == gm.WRITE for e in bufR_edges))
        # and the surviving read edge is NOT dashed (confirmed read, not unused)
        self.assertTrue(all(e[3] is False for e in bufR_edges))


if __name__ == '__main__':
    unittest.main()
