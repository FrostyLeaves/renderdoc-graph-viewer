# -*- coding: utf-8 -*-
"""Pass grouping (graph_model.build_passes): consecutive same-target draw
merging, marker aggregation (graphics priority), clear folding into the
following same-target pass, duplicate-name #N suffixes, and transfer/present
node kinds."""
import unittest

from renderdoc_graph_viewer.graph_model import build_passes
from tests.fakes import draw, dispatch, clear, transfer, present


class TestGrouping(unittest.TestCase):
    def test_consecutive_draws_same_targets_merge(self):
        passes = build_passes([
            draw(10, ('A',), 'D', markers=('Frame', 'GBuffer')),
            draw(11, ('A',), 'D', markers=('Frame', 'GBuffer')),
            draw(12, ('B',), 'D', markers=('Frame', 'Lighting')),
        ])
        self.assertEqual(len(passes), 2)
        self.assertEqual(passes[0].name, 'GBuffer')
        self.assertEqual(passes[0].first_eid, 10)
        self.assertEqual(passes[0].last_eid, 11)
        self.assertEqual(passes[0].action_count, 2)
        self.assertEqual(passes[1].name, 'Lighting')
        self.assertEqual(passes[0].kind, 'graphics')

    def test_dispatch_merges_only_within_same_marker(self):
        passes = build_passes([
            dispatch(10, markers=('SSAO',)),
            dispatch(11, markers=('SSAO',)),
            dispatch(12, markers=('Blur',)),
        ])
        self.assertEqual(len(passes), 2)
        self.assertEqual(passes[0].kind, 'compute')
        self.assertEqual(passes[0].action_count, 2)
        self.assertEqual(passes[1].name, 'Blur')

    def test_same_marker_aggregates_mixed_kinds(self):
        # marker is the grouping unit: a draw and a dispatch inside the same
        # marker form one node (kind: graphics takes priority)
        passes = build_passes([
            draw(10, ('A',), markers=('M',)),
            dispatch(11, markers=('M',)),
        ])
        self.assertEqual(len(passes), 1)
        self.assertEqual(passes[0].kind, 'graphics')
        self.assertEqual(passes[0].action_count, 2)

    def test_markerless_draw_then_dispatch_split(self):
        passes = build_passes([
            draw(10, ('A',)),
            dispatch(11),
        ])
        self.assertEqual(len(passes), 2)

    def test_clear_folds_into_following_pass_with_same_target(self):
        passes = build_passes([
            clear(5, ('A',)),
            draw(10, ('A',), 'D', markers=('GBuffer',)),
        ])
        self.assertEqual(len(passes), 1)
        self.assertEqual(passes[0].first_eid, 5)
        self.assertEqual(passes[0].action_count, 2)
        self.assertEqual(passes[0].name, 'GBuffer')

    def test_clear_of_unrelated_target_stays_standalone(self):
        passes = build_passes([
            clear(5, ('X',)),
            draw(10, ('A',), markers=('GBuffer',)),
        ])
        self.assertEqual(len(passes), 2)
        self.assertEqual(passes[0].kind, 'transfer')

    def test_duplicate_marker_names_get_suffix(self):
        # non-consecutive same-name markers stay separate nodes with suffixes
        # (consecutive same-name markers merge into one node by design)
        passes = build_passes([
            draw(10, ('A',), markers=('Shadow',)),
            draw(20, ('B',), markers=('GBuffer',)),
            draw(30, ('A',), markers=('Shadow',)),
        ])
        self.assertEqual([p.name for p in passes],
                         ['Shadow', 'GBuffer', 'Shadow #2'])

    def test_fallback_names_without_markers(self):
        passes = build_passes(
            [draw(10, ('A',)), dispatch(20)],
            res_names={'A': 'MainColor'})
        self.assertEqual(passes[0].name, 'Pass #1 (MainColor)')
        self.assertEqual(passes[1].name, 'Compute #2')

    def test_transfer_and_present_nodes(self):
        passes = build_passes([
            transfer(10, src='A', dst='B', name='CopyResource'),
            present(20, src='B'),
        ])
        self.assertEqual(passes[0].kind, 'transfer')
        self.assertEqual(passes[0].name, 'CopyResource')
        self.assertEqual(passes[1].kind, 'present')
        self.assertEqual(passes[1].name, 'Present')


if __name__ == '__main__':
    unittest.main()
