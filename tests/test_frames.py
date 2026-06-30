# -*- coding: utf-8 -*-
"""Nested-frame mode: marker depth defines the LEAF level; shallower
markers become nested frames; frames collapse back into aggregate nodes."""

import unittest

from renderdoc_graph_viewer.graph_model import build_passes, build_graph
from tests.fakes import draw, dispatch

RES_INFO = {
    'GB': {'kind': 'color', 'info': {}},
    'HDR': {'kind': 'color', 'info': {}},
    'M': {'kind': 'uav_tex', 'info': {}},
}


def _scene_leaves():
    # Frame/Camera/{Gbuffer,Lighting}; Frame/Sim/{Step}
    return [
        draw(10, ('GB',), markers=('Frame', 'Camera', 'Gbuffer')),
        draw(20, ('HDR',), markers=('Frame', 'Camera', 'Lighting')),
        dispatch(30, markers=('Frame', 'Sim', 'Step')),
    ]


class TestCollapsedGrouping(unittest.TestCase):
    def test_no_collapse_keeps_leaf_depth(self):
        passes = build_passes(_scene_leaves(), marker_depth=3)
        self.assertEqual([p.name for p in passes],
                         ['Gbuffer', 'Lighting', 'Step'])
        self.assertFalse(any(p.collapsed_frame for p in passes))

    def test_collapsed_frame_aggregates_subtree(self):
        passes = build_passes(_scene_leaves(), marker_depth=3,
                              collapsed={('Frame', 'Camera')})
        self.assertEqual([p.name for p in passes], ['Camera', 'Step'])
        cam = passes[0]
        self.assertTrue(cam.collapsed_frame)
        self.assertEqual(cam.marker_path, ('Frame', 'Camera'))
        self.assertEqual(cam.action_count, 2)
        self.assertFalse(passes[1].collapsed_frame)

    def test_shallowest_collapsed_ancestor_wins(self):
        passes = build_passes(_scene_leaves(), marker_depth=3,
                              collapsed={('Frame',),
                                         ('Frame', 'Camera')})
        self.assertEqual([p.name for p in passes], ['Frame'])
        self.assertTrue(passes[0].collapsed_frame)


class TestFrameAssignment(unittest.TestCase):
    def test_pass_frame_path_is_parent_prefix(self):
        passes = build_passes(_scene_leaves(), marker_depth=3)
        self.assertEqual(passes[0].frame_path, ('Frame', 'Camera'))
        self.assertEqual(passes[2].frame_path, ('Frame', 'Sim'))

    def test_resource_lands_in_lowest_common_frame(self):
        passes = build_passes(_scene_leaves(), marker_depth=3)
        fg = build_graph(passes, {
            'GB': [(10, 'ColorTarget'), (20, 'PS_Resource')],
            'M': [(30, 'CS_RWResource')],
        }, RES_INFO)
        by_key = {}
        for n in fg.resources:
            by_key.setdefault(n.res_key, []).append(n)
        # GB: written in Camera/Gbuffer, read in Camera/Lighting
        #     -> shared inside the Camera frame
        for n in by_key['GB']:
            self.assertEqual(n.frame_path, ('Frame', 'Camera'))
        # M: touched only by Sim/Step -> private to the Sim frame
        for n in by_key['M']:
            self.assertEqual(n.frame_path, ('Frame', 'Sim'))

    def test_cross_top_frame_resource_goes_to_root(self):
        leaves = [
            draw(10, ('GB',), markers=('Frame', 'Camera', 'Gbuffer')),
            dispatch(30, markers=('Other', 'Sim')),
        ]
        passes = build_passes(leaves, marker_depth=2)
        fg = build_graph(passes, {
            'GB': [(10, 'ColorTarget'), (30, 'CS_Resource')],
        }, RES_INFO)
        for n in fg.resources:
            self.assertEqual(n.frame_path, ())

    def test_frame_paths_exported(self):
        passes = build_passes(_scene_leaves(), marker_depth=3)
        fg = build_graph(passes, {
            'GB': [(10, 'ColorTarget'), (20, 'PS_Resource')],
        }, RES_INFO)
        self.assertIn(('Frame',), fg.frame_paths)
        self.assertIn(('Frame', 'Camera'), fg.frame_paths)
        self.assertIn(('Frame', 'Sim'), fg.frame_paths)
        self.assertNotIn(('Frame', 'Camera', 'Gbuffer'), fg.frame_paths)


if __name__ == '__main__':
    unittest.main()
