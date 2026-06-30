# -*- coding: utf-8 -*-
"""Default (merged) graph mode: one node per resource, DAG-safe rank edges,
orphan detection (hidden by the UI, kept in the model). Pass-level
aggregation is covered by tests/test_depth.py (marker-depth grouping)."""

import unittest

from renderdoc_graph_viewer.graph_model import build_passes, build_graph
from tests.fakes import draw, dispatch, transfer, present

RES_INFO = {
    'A': {'kind': 'color', 'info': {}},
    'B': {'kind': 'color', 'info': {}},
    'SC': {'kind': 'swapchain', 'info': {}},
    'M': {'kind': 'uav_tex', 'info': {}},
    'GB': {'kind': 'color', 'info': {}},
    'HDR': {'kind': 'color', 'info': {}},
}


def _passes(*leaves):
    return build_passes(list(leaves))


def _edge_set(fg):
    return set((e.src_id, e.dst_id, e.kind) for e in fg.edges)


def _rank_set(fg):
    return set((e.src_id, e.dst_id) for e in fg.rank_edges)


class TestMergedResources(unittest.TestCase):
    def test_single_node_despite_write_after_read(self):
        passes = _passes(draw(10, ('A',), markers=('P1',)),
                         draw(20, ('B',), markers=('P2',)),
                         draw(30, ('A',), markers=('P3',)))
        fg = build_graph(passes, {
            'A': [(10, 'ColorTarget'), (20, 'PS_Resource'), (30, 'ColorTarget')],
        }, RES_INFO)
        self.assertEqual(len(fg.resources), 1)
        node = fg.resources[0]
        self.assertEqual(node.writer_ids, ['p0', 'p2'])
        self.assertEqual(node.reader_ids, ['p1'])
        self.assertEqual(node.last_write_eid, 30)
        self.assertEqual(_edge_set(fg), {
            ('p0', node.id, 'write'),
            (node.id, 'p1', 'read'),
            ('p2', node.id, 'write'),
        })

    def test_rank_edges_for_write_after_read(self):
        passes = _passes(draw(10, ('A',), markers=('P1',)),
                         draw(20, ('B',), markers=('P2',)),
                         draw(30, ('A',), markers=('P3',)))
        fg = build_graph(passes, {
            'A': [(10, 'ColorTarget'), (20, 'PS_Resource'), (30, 'ColorTarget')],
        }, RES_INFO)
        node = fg.resources[0]
        ranks = _rank_set(fg)
        self.assertIn(('p0', node.id), ranks)     # first pure writer anchors it
        self.assertIn((node.id, 'p1'), ranks)     # reader comes after
        self.assertIn(('p0', 'p1'), ranks)        # touch chain
        self.assertIn(('p1', 'p2'), ranks)
        self.assertNotIn(('p2', node.id), ranks)  # later write: drawn, not ranked

    def test_self_rw_without_other_writer_is_output_only(self):
        # a resource the node both reads and writes, with no other producer
        # anywhere, is logically this node's OUTPUT - the self-read expresses
        # no inter-node relationship and is dropped
        passes = _passes(dispatch(10, markers=('Sim',)))
        fg = build_graph(passes, {
            'A': [(10, 'CS_RWResource')],
        }, RES_INFO)
        self.assertEqual(len(fg.resources), 1)
        node = fg.resources[0]
        self.assertFalse(node.imported)
        self.assertEqual(node.reader_ids, [])
        self.assertEqual(_edge_set(fg), {('p0', node.id, 'write')})
        self.assertEqual(_rank_set(fg), {('p0', node.id)})

    def test_self_rw_with_downstream_reader_keeps_their_edge(self):
        passes = _passes(dispatch(10, markers=('Sim',)),
                         draw(20, ('B',), markers=('Use',)))
        fg = build_graph(passes, {
            'A': [(10, 'CS_RWResource'), (20, 'PS_Resource')],
            'B': [(20, 'ColorTarget')],
        }, RES_INFO)
        by_key = dict((n.res_key, n) for n in fg.resources)
        a = by_key['A']
        es = _edge_set(fg)
        self.assertIn(('p0', a.id, 'write'), es)
        self.assertIn((a.id, 'p1', 'read'), es)      # other node's read kept
        self.assertNotIn((a.id, 'p0', 'read'), es)   # self-read dropped

    def test_self_rw_with_external_writer_keeps_read(self):
        # someone else produced it (externally_written): the read is a real
        # relationship (e.g. TAA history / scope input) - keep it
        passes = _passes(dispatch(10, markers=('Sim',)))
        fg = build_graph(passes, {
            'A': [(10, 'CS_RWResource')],
        }, RES_INFO, externally_written={'A'})
        es = _edge_set(fg)
        self.assertEqual(len(fg.resources), 1)  # merged mode: single node
        node = fg.resources[0]
        self.assertIn((node.id, 'p0', 'read'), es)
        self.assertIn(('p0', node.id, 'write'), es)

    def test_reader_before_first_write_gets_no_rank_edge(self):
        # WAR pattern: p0 reads imported content, p1 overwrites it.
        passes = _passes(draw(10, ('B',), markers=('P1',)),
                         draw(20, ('A',), markers=('P2',)))
        fg = build_graph(passes, {
            'A': [(10, 'PS_Resource'), (20, 'ColorTarget')],
            'B': [(10, 'ColorTarget')],
        }, RES_INFO)
        by_key = dict((n.res_key, n) for n in fg.resources)
        a = by_key['A']
        self.assertFalse(a.imported)
        ranks = _rank_set(fg)
        self.assertIn(('p1', a.id), ranks)      # anchored at its writer
        self.assertNotIn((a.id, 'p0'), ranks)   # early read not ranked
        self.assertIn((a.id, 'p0', 'read'), _edge_set(fg))  # but still drawn

    def test_imported_only_when_no_writers(self):
        passes = _passes(draw(10, ('B',), markers=('P1',)))
        fg = build_graph(passes, {
            'A': [(10, 'PS_Resource')],
            'B': [(10, 'ColorTarget')],
        }, RES_INFO)
        by_key = dict((n.res_key, n) for n in fg.resources)
        self.assertTrue(by_key['A'].imported)
        self.assertFalse(by_key['B'].imported)

    def test_present_links_and_ranks(self):
        passes = _passes(draw(10, ('SC',), markers=('Final',)),
                         present(20, src='SC'))
        fg = build_graph(passes, {
            'SC': [(10, 'ColorTarget'), (20, 'Present')],
        }, RES_INFO)
        node = fg.resources[0]
        self.assertIn((node.id, 'p1', 'read'), _edge_set(fg))
        self.assertIn((node.id, 'p1'), _rank_set(fg))

    def test_present_without_source_hint_is_not_guessed_in_model(self):
        passes = _passes(draw(10, ('SC',), markers=('Final',)),
                         present(20))
        fg = build_graph(passes, {
            'SC': [(10, 'ColorTarget')],
        }, RES_INFO)
        present_pass = next(p for p in fg.passes if p.kind == 'present')
        incoming = [e for e in fg.edges if e.dst_id == present_pass.id]
        self.assertEqual(incoming, [])


class TestGapBucketing(unittest.TestCase):
    """Usage events between pass intervals."""

    def _two_passes(self):
        return build_passes([
            draw(10, ('A',), markers=('P1',)),
            draw(11, ('A',), markers=('P1',)),
            draw(30, ('B',), markers=('P2',)),
            draw(31, ('B',), markers=('P2',)),
        ])

    def test_gap_clear_attaches_to_next_pass(self):
        # loadOp clear at vkCmdBeginRenderPass
        passes = self._two_passes()
        fg = build_graph(passes, {
            'B': [(12, 'Clear'), (30, 'ColorTarget')],
        }, RES_INFO)
        node = fg.resources[0]
        self.assertEqual(node.writer_ids, ['p1'])
        self.assertEqual(len(fg.resources), 1)

    def test_gap_write_attaches_to_nearest_prev(self):
        # storeOp/resolve writes right after a pass attach to it
        passes = self._two_passes()
        fg = build_graph(passes, {
            'A': [(10, 'ColorTarget'), (12, 'ColorTarget')],
        }, RES_INFO)
        node = fg.resources[0]
        self.assertEqual(node.writer_ids, ['p0'])
        self.assertEqual(node.last_write_eid, 12)

    def test_gap_attachment_write_goes_to_prev_even_when_next_is_closer(self):
        # storeOp write after renderpass end
        passes = self._two_passes()
        fg = build_graph(passes, {
            'A': [(10, 'ColorTarget'), (29, 'ColorTarget')],
        }, RES_INFO)
        node = fg.resources[0]
        self.assertEqual(node.writer_ids, ['p0'])
        self.assertEqual(node.last_write_eid, 29)

    def test_gap_resolve_and_depth_go_to_prev(self):
        passes = self._two_passes()
        fg = build_graph(passes, {
            'A': [(10, 'ColorTarget'), (28, 'ResolveDst')],
            'B': [(11, 'DepthStencilTarget'), (29, 'DepthStencilTarget')],
        }, RES_INFO)
        by_key = dict((n.res_key, n) for n in fg.resources)
        self.assertEqual(by_key['A'].writer_ids, ['p0'])
        self.assertEqual(by_key['B'].writer_ids, ['p0'])

    def test_gap_copy_still_goes_to_nearest(self):
        # non-attachment gap events keep the nearest rule
        passes = self._two_passes()
        fg = build_graph(passes, {
            'B': [(29, 'CopyDst'), (30, 'ColorTarget')],
        }, RES_INFO)
        node = fg.resources[0]
        self.assertEqual(node.writer_ids, ['p1'])

    def test_outside_frame_events_still_dropped(self):
        passes = self._two_passes()
        fg = build_graph(passes, {
            'A': [(10, 'ColorTarget'), (999, 'PS_Resource')],
        }, RES_INFO)
        node = fg.resources[0]
        self.assertEqual(node.reader_ids, [])

    def test_storeop_discard_at_end_boundary_goes_to_prev_pass(self):
        # storeOp=DONT_CARE discard at an END boundary
        res_info = {
            'Color': {'kind': 'color', 'info': {}},
            'Depth': {'kind': 'depth', 'info': {}},
            'SC':    {'kind': 'swapchain', 'info': {}},
        }
        passes = build_passes([
            draw(10, ('Color',), depth='Depth', markers=('Shade',)),
            present(20, src='SC'),
        ])
        fg = build_graph(passes, {
            'Color': [(10, 'ColorTarget')],
            'Depth': [(10, 'DepthStencilTarget'), (15, 'Discard')],
            'SC': [(20, 'Present')],
        }, res_info, boundaries={15: 'end'})
        by_key = dict((n.res_key, n) for n in fg.resources)
        present_pass = next(p for p in fg.passes if p.kind == 'present')
        graphics_pass = next(p for p in fg.passes if p.kind == 'graphics')
        # the spurious output is gone: present no longer "writes" the depth
        self.assertNotIn(present_pass.id, by_key['Depth'].writer_ids)
        self.assertIn(graphics_pass.id, by_key['Depth'].writer_ids)
        # present keeps its real input
        self.assertIn((by_key['SC'].id, present_pass.id, 'read'), _edge_set(fg))
        self.assertIn(present_pass.id, by_key['SC'].reader_ids)

    def test_loadop_discard_at_begin_boundary_goes_to_next_pass(self):
        # loadOp=DONT_CARE discard at a BEGIN boundary
        passes = self._two_passes()       # P1 @10,11 ; P2 @30,31 ; gap (11,30)
        fg = build_graph(passes, {
            'B': [(20, 'Discard'), (30, 'ColorTarget')],
        }, RES_INFO, boundaries={20: 'begin'})
        node = fg.resources[0]
        self.assertEqual(node.writer_ids, ['p1'])   # next pass, not P1


class TestOrphanDetection(unittest.TestCase):
    def test_orphans_flagged_not_pruned_with_rank_hint(self):
        passes = _passes(draw(10, ('A',), markers=('P1',)),
                         transfer(20, src='X', dst='Y', name='vkCmdCopyImage(1)'))
        fg = build_graph(passes, {
            'A': [(10, 'ColorTarget')],
        }, RES_INFO)
        self.assertEqual(len(fg.passes), 2)  # model keeps them; UI hides
        self.assertEqual(fg.orphan_pass_ids, {'p1'})
        self.assertIn(('p0', 'p1'), _rank_set(fg))  # placed after nearest toucher


if __name__ == '__main__':
    unittest.main()
