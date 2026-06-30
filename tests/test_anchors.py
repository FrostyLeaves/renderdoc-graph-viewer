# -*- coding: utf-8 -*-
"""Pure edge-anchor fan-out distribution (anchors.assign_anchor_fractions)."""
import unittest

from renderdoc_graph_viewer.ui.anchors import assign_anchor_fractions


class TestAssignAnchorFractions(unittest.TestCase):
    def test_single_edge_centres_both_ends(self):
        out = assign_anchor_fractions([(0, 'a', 'b', 'r', 10.0, 20.0)])
        self.assertEqual(out, {0: (0.5, 0.5)})

    def test_fan_out_ordered_by_other_end_y(self):
        # three edges leave node 'a' to b/c/d; the 'a' side splits 1/4,2/4,3/4
        # ordered by the dst y (sort_y_out).
        edges = [
            (0, 'a', 'b', 'r', 30.0, 0.0),
            (1, 'a', 'c', 'r', 10.0, 0.0),
            (2, 'a', 'd', 'r', 20.0, 0.0),
        ]
        out = assign_anchor_fractions(edges)
        self.assertAlmostEqual(out[1][0], 0.25)   # c (y=10) first
        self.assertAlmostEqual(out[2][0], 0.50)   # d (y=20)
        self.assertAlmostEqual(out[0][0], 0.75)   # b (y=30) last
        for k in (0, 1, 2):                        # sole edge into each dst
            self.assertAlmostEqual(out[k][1], 0.5)

    def test_in_side_fans_independently(self):
        edges = [
            (0, 'x', 'z', 'r', 0.0, 5.0),
            (1, 'y', 'z', 'r', 0.0, 1.0),
        ]
        out = assign_anchor_fractions(edges)
        self.assertAlmostEqual(out[1][1], 1.0 / 3.0)   # y (y=1) first
        self.assertAlmostEqual(out[0][1], 2.0 / 3.0)   # x (y=5) second

    def test_tie_broken_by_src_dst_kind(self):
        edges = [
            (0, 'a', 'c', 'r', 5.0, 0.0),
            (1, 'a', 'b', 'r', 5.0, 0.0),
        ]
        out = assign_anchor_fractions(edges)
        self.assertAlmostEqual(out[1][0], 1.0 / 3.0)   # a->b first
        self.assertAlmostEqual(out[0][0], 2.0 / 3.0)   # a->c


if __name__ == '__main__':
    unittest.main()
