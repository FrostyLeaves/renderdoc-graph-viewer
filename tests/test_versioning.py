# -*- coding: utf-8 -*-
"""Versioned graph building (graph_model.build_graph, versioned=True): write-
after-read version splitting, version labels / counts, imported detection,
internal self-RW folding, and present linking to the latest version."""
import unittest

from renderdoc_graph_viewer.graph_model import build_passes, build_graph
from tests.fakes import draw, dispatch, present

RES_INFO = {
    'A': {'kind': 'color', 'info': {'dims': '128x128'}},
    'B': {'kind': 'color', 'info': {}},
    'SC': {'kind': 'swapchain', 'info': {}},
}


def _passes(*leaves):
    return build_passes(list(leaves))


def _edge_set(fg):
    return set((e.src_id, e.dst_id, e.kind) for e in fg.edges)


class TestVersioning(unittest.TestCase):
    def test_write_then_read(self):
        passes = _passes(draw(10, ('A',), markers=('P1',)),
                         draw(20, ('B',), markers=('P2',)))
        fg = build_graph(passes, {
            'A': [(10, 'ColorTarget'), (20, 'PS_Resource')],
        }, RES_INFO, versioned=True)
        self.assertEqual(len(fg.resources), 1)
        node = fg.resources[0]
        self.assertEqual(node.version, 1)
        self.assertFalse(node.imported)
        self.assertEqual(node.last_write_eid, 10)
        self.assertEqual(_edge_set(fg),
                         {('p0', node.id, 'write'), (node.id, 'p1', 'read')})

    def test_war_creates_new_version(self):
        passes = _passes(draw(10, ('A',), markers=('P1',)),
                         draw(20, ('B',), markers=('P2',)),
                         draw(30, ('A',), markers=('P3',)))
        fg = build_graph(passes, {
            'A': [(10, 'ColorTarget'), (20, 'PS_Resource'), (30, 'ColorTarget')],
        }, RES_INFO, versioned=True)
        self.assertEqual(len(fg.resources), 2)
        v1, v2 = fg.resources
        self.assertEqual((v1.version, v2.version), (1, 2))
        es = _edge_set(fg)
        self.assertIn(('p0', v1.id, 'write'), es)
        self.assertIn((v1.id, 'p1', 'read'), es)
        self.assertIn(('p2', v2.id, 'write'), es)
        self.assertNotIn((v2.id, 'p1', 'read'), es)

    def test_version_count_single_write(self):
        passes = _passes(draw(10, ('A',), markers=('P1',)))
        fg = build_graph(passes, {
            'A': [(10, 'ColorTarget')],
        }, RES_INFO, versioned=True)
        self.assertEqual(fg.resources[0].version_count, 1)

    def test_version_count_multi_write(self):
        passes = _passes(draw(10, ('A',), markers=('P1',)),
                         draw(20, ('B',), markers=('P2',)),
                         draw(30, ('A',), markers=('P3',)))
        fg = build_graph(passes, {
            'A': [(10, 'ColorTarget'), (20, 'PS_Resource'), (30, 'ColorTarget')],
        }, RES_INFO, versioned=True)
        v1, v2 = fg.resources
        # both versions know the resource was written more than once
        self.assertEqual((v1.version_count, v2.version_count), (2, 2))

    def test_waw_without_read_shares_version(self):
        passes = _passes(draw(10, ('A',), markers=('P1',)),
                         draw(20, ('A',), depth='D', markers=('P2',)))
        fg = build_graph(passes, {
            'A': [(10, 'ColorTarget'), (20, 'ColorTarget')],
        }, RES_INFO, versioned=True)
        self.assertEqual(len(fg.resources), 1)
        node = fg.resources[0]
        self.assertEqual(node.writer_ids, ['p0', 'p1'])
        self.assertEqual(node.last_write_eid, 20)

    def test_rw_chain_ping_pong(self):
        passes = _passes(dispatch(10, markers=('Sim1',)),
                         dispatch(20, markers=('Sim2',)))
        fg = build_graph(passes, {
            'A': [(10, 'CS_RWResource'), (20, 'CS_RWResource')],
        }, RES_INFO, versioned=True)
        # imported v1 (read by p0) -> v2 (written p0, read p1) -> v3 (written p1)
        self.assertEqual(len(fg.resources), 3)
        v1, v2, v3 = fg.resources
        self.assertTrue(v1.imported)
        es = _edge_set(fg)
        self.assertIn((v1.id, 'p0', 'read'), es)
        self.assertIn(('p0', v2.id, 'write'), es)
        self.assertIn((v2.id, 'p1', 'read'), es)
        self.assertIn(('p1', v3.id, 'write'), es)

    def test_read_only_resource_is_imported(self):
        passes = _passes(draw(10, ('B',), markers=('P1',)))
        fg = build_graph(passes, {
            'A': [(10, 'PS_Resource')],
        }, RES_INFO, versioned=True)
        node = fg.resources[0]
        self.assertTrue(node.imported)
        self.assertIsNone(node.last_write_eid)
        self.assertEqual(node.first_read_eid, 10)

    def test_usage_outside_passes_dropped_and_ignored_usages_skipped(self):
        passes = _passes(draw(10, ('A',), markers=('P1',)))
        fg = build_graph(passes, {
            'A': [(10, 'ColorTarget'), (10, 'Barrier'), (999, 'PS_Resource')],
        }, RES_INFO, versioned=True)
        self.assertEqual(len(fg.resources), 1)
        self.assertEqual(_edge_set(fg), {('p0', fg.resources[0].id, 'write')})

    def test_present_links_to_latest_version(self):
        passes = _passes(draw(10, ('SC',), markers=('Final',)),
                         present(20, src='SC'))
        fg = build_graph(passes, {
            'SC': [(10, 'ColorTarget'), (20, 'Present')],
        }, RES_INFO, versioned=True)
        node = fg.resources[0]
        es = _edge_set(fg)
        self.assertIn(('p0', node.id, 'write'), es)
        self.assertIn((node.id, 'p1', 'read'), es)
        prs = [e for e in fg.edges if e.dst_id == 'p1']
        self.assertEqual(prs[0].usages, [(20, 'Present')])

    def test_write_only_resource_is_exposed(self):
        # a resource that is written but never read anywhere is NOT
        # internal - surface it so the user can judge whether it is a
        # readback or a problem
        passes = _passes(draw(10, ('A',), markers=('P1',)))
        fg = build_graph(passes, {
            'A': [(10, 'ColorTarget')],
        }, RES_INFO, versioned=True)
        self.assertFalse(fg.resources[0].internal)

    def test_pure_self_rw_is_internal(self):
        # a resource only its own writer reads (self-RW folded to
        # write-only by the rule) is a private working set
        passes = _passes(dispatch(10, markers=('FSR',)))
        fg = build_graph(passes, {
            'A': [(10, 'CS_RWResource')],
        }, RES_INFO, versioned=True)
        nodes = [n for n in fg.resources if n.writer_ids]
        self.assertTrue(all(n.internal for n in nodes))

    def test_resource_label(self):
        passes = _passes(draw(10, ('A',), markers=('P1',)),
                         draw(20, ('B',), markers=('P2',)),
                         draw(30, ('A',), markers=('P3',)))
        fg = build_graph(passes, {
            'A': [(10, 'ColorTarget'), (20, 'PS_Resource'), (30, 'ColorTarget')],
        }, RES_INFO, res_names={'A': 'HDR'}, versioned=True)
        # multi-version resource: EVERY version is labelled, including v1
        self.assertEqual(fg.resources[0].label(), 'HDR (v1)')
        self.assertEqual(fg.resources[1].label(), 'HDR (v2)')

    def test_single_version_label_has_no_suffix(self):
        passes = _passes(draw(10, ('A',), markers=('P1',)))
        fg = build_graph(passes, {
            'A': [(10, 'ColorTarget')],
        }, RES_INFO, res_names={'A': 'HDR'}, versioned=True)
        self.assertEqual(fg.resources[0].label(), 'HDR')


if __name__ == '__main__':
    unittest.main()
