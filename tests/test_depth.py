# -*- coding: utf-8 -*-
"""Marker-depth grouping: leaves group by their semantic marker path
truncated to the requested depth; inner sub-marker I/O aggregates into the
ancestor node. Depth None = unlimited (full path)."""

import unittest

from renderdoc_graph_viewer.graph_model import build_passes, build_graph
from tests.fakes import draw, dispatch, transfer, present

RES_INFO = {
    'SC': {'kind': 'color', 'info': {}},
    'M': {'kind': 'uav_tex', 'info': {}},
    'GB': {'kind': 'color', 'info': {}},
    'HDR': {'kind': 'color', 'info': {}},
}


def _edge_set(fg):
    return set((e.src_id, e.dst_id, e.kind) for e in fg.edges)


class TestDepthGrouping(unittest.TestCase):
    def _bloom_leaves(self):
        return [
            dispatch(10, markers=('Bloom', 'Down1')),
            dispatch(20, markers=('Bloom', 'Down2')),
            draw(30, ('HDR',), markers=('Lighting',)),
        ]

    def test_depth1_aggregates_nested_markers(self):
        passes = build_passes(self._bloom_leaves(), marker_depth=1)
        self.assertEqual(len(passes), 2)
        bloom, lighting = passes
        self.assertEqual(bloom.name, 'Bloom')
        self.assertEqual(bloom.kind, 'compute')
        self.assertEqual((bloom.first_eid, bloom.last_eid), (10, 20))
        self.assertEqual(bloom.action_count, 2)
        self.assertEqual(lighting.name, 'Lighting')

    def test_depth2_drills_down(self):
        passes = build_passes(self._bloom_leaves(), marker_depth=2)
        self.assertEqual([p.name for p in passes],
                         ['Down1', 'Down2', 'Lighting'])

    def test_unlimited_depth_uses_full_path(self):
        passes = build_passes(self._bloom_leaves(), marker_depth=None)
        self.assertEqual([p.name for p in passes],
                         ['Down1', 'Down2', 'Lighting'])

    def test_depth1_mixed_kinds_aggregate_as_graphics(self):
        passes = build_passes([
            dispatch(10, markers=('Post', 'AO')),
            draw(20, ('HDR',), markers=('Post', 'Combine')),
            transfer(30, src='HDR', dst='SC', markers=('Post', 'CopyOut')),
        ], marker_depth=1)
        self.assertEqual(len(passes), 1)
        self.assertEqual(passes[0].name, 'Post')
        self.assertEqual(passes[0].kind, 'graphics')
        self.assertEqual(passes[0].action_count, 3)

    def test_markerless_leaves_keep_fine_grouping(self):
        passes = build_passes([
            dispatch(10),
            dispatch(20),
            draw(30, ('HDR',)),
        ], marker_depth=1)
        self.assertEqual(len(passes), 2)  # two dispatches merge; draw separate
        self.assertEqual(passes[0].kind, 'compute')
        self.assertEqual(passes[1].kind, 'graphics')

    def test_present_never_aggregated_into_marker(self):
        leaves = [
            draw(10, ('SC',), markers=('Final',)),
            present(20, src='SC'),
        ]
        leaves[1].marker_path = ('Final',)  # even when inside the marker
        passes = build_passes(leaves, marker_depth=1)
        self.assertEqual(len(passes), 2)
        self.assertEqual(passes[1].kind, 'present')

    def test_nonconsecutive_same_name_markers_get_suffix(self):
        passes = build_passes([
            draw(10, ('GB',), markers=('Shadow',)),
            draw(20, ('HDR',), markers=('Lighting',)),
            draw(30, ('GB',), markers=('Shadow',)),
        ], marker_depth=1)
        self.assertEqual([p.name for p in passes],
                         ['Shadow', 'Lighting', 'Shadow #2'])

    def test_depth1_io_with_self_rw_collapses_to_output(self):
        # prefilter reads SC, chain churns M; M has no other producer, so
        # per the rule the aggregated node treats M as pure output
        passes = build_passes([
            dispatch(10, markers=('Bloom', 'Prefilter')),
            dispatch(20, markers=('Bloom', 'Down1')),
        ], marker_depth=1)
        self.assertEqual(len(passes), 1)
        fg = build_graph(passes, {
            'SC': [(10, 'CS_Resource')],
            'M': [(10, 'CS_RWResource'), (20, 'CS_RWResource')],
        }, RES_INFO)
        p = fg.passes[0]
        by_key = dict((n.res_key, n) for n in fg.resources)
        self.assertEqual(_edge_set(fg), {
            (by_key['SC'].id, p.id, 'read'),
            (p.id, by_key['M'].id, 'write'),
        })

    def test_internal_handoff_collapses_to_output(self):
        # GBuffer written and read entirely inside one depth-1 node with no
        # other producer: shown as the node's output only (the rule)
        passes = build_passes([
            draw(10, ('GB',), markers=('Camera', 'GBuffer')),
            draw(20, ('HDR',), markers=('Camera', 'Lighting')),
        ], marker_depth=1)
        self.assertEqual(len(passes), 1)
        fg = build_graph(passes, {
            'GB': [(10, 'ColorTarget'), (20, 'PS_Resource')],
            'HDR': [(20, 'ColorTarget')],
        }, RES_INFO)
        p = fg.passes[0]
        by_key = dict((n.res_key, n) for n in fg.resources)
        self.assertIn((p.id, by_key['GB'].id, 'write'), _edge_set(fg))
        self.assertNotIn((by_key['GB'].id, p.id, 'read'), _edge_set(fg))


if __name__ == '__main__':
    unittest.main()
