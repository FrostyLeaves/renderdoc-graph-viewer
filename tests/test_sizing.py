# -*- coding: utf-8 -*-
"""Pure node-sizing geometry (sizing.node_size / measure_member_rows)."""
import types
import unittest

from renderdoc_graph_viewer.ui import sizing
from renderdoc_graph_viewer.graph_model import (CAT_COMPUTE, CAT_PORTAL,
                                                RES_BUFFER, RES_COLOR)


def tw(text, bold=False):
    """Deterministic fake QFontMetrics width; bold is wider."""
    return len(text) * (10.0 if bold else 8.0)


def _pass(name='Pass', kind='graphics', first=1, last=3, count=2,
          drillable=False, members=None):
    return types.SimpleNamespace(
        kind=kind, name=name, first_eid=first, last_eid=last,
        action_count=count, drillable=drillable, bundle_members=members)


def _res(label='Tex', kind=RES_COLOR, version=1, members=None, info=None):
    ns = types.SimpleNamespace(
        res_kind=kind, version=version, bundle_members=members, name=label,
        info=info or {'dims': '256x256', 'format': 'rgba'})
    ns.label = lambda: label
    return ns


class TestPassSize(unittest.TestCase):
    def test_simple_pass_fixed_height_and_min_width(self):
        w, h = sizing.node_size(_pass(), True, tw, lambda n: False)
        self.assertEqual(h, 46.0)
        self.assertGreaterEqual(w, 130.0)

    def test_type_kind_and_drillable_do_not_change_width(self):
        # pass title width excludes kind/drill indicators
        base = sizing.node_size(_pass(name='Long pass name'),
                                True, tw, lambda n: False)[0]
        comp = sizing.node_size(_pass(name='Long pass name', kind=CAT_COMPUTE),
                                True, tw, lambda n: False)[0]
        drill = sizing.node_size(_pass(name='Long pass name', drillable=True),
                                 True, tw, lambda n: False)[0]
        self.assertEqual(comp, base)
        self.assertEqual(drill, base)

    def test_portal_node_sized_for_external_scope_subrow(self):
        # a portal's sub-row is 'External scope EID a-b', wider than the plain
        # 'EID a-b  (n)', so a short-named portal is sized by that sub-row.
        wp = sizing.node_size(_pass(name='P', kind=CAT_PORTAL, first=10,
                                    last=20), True, tw, lambda n: False)[0]
        wn = sizing.node_size(_pass(name='P', first=10, last=20),
                              True, tw, lambda n: False)[0]
        self.assertGreater(wp, wn)
        self.assertGreaterEqual(wp, tw('External scope EID 10-20'))

    def test_pass_bundle_height_counts_rows(self):
        members = ['m%d' % i for i in range(5)]
        _w, h = sizing.node_size(_pass(members=members),
                                 True, tw, lambda n: False)
        self.assertEqual(h, sizing.PASS_BUNDLE_ROWS_Y
                         + 5 * sizing.BUNDLE_ROW_H + 6.0)


class TestResourceSize(unittest.TestCase):
    def test_plain_resource_height(self):
        _w, h = sizing.node_size(_res(), False, tw, lambda n: False)
        self.assertEqual(h, 42.0)

    def test_version_badge_widens_by_badge_w(self):
        long = 'A resource with a comfortably long label'
        v1 = sizing.node_size(_res(label=long, version=1),
                              False, tw, lambda n: False)[0]
        v2 = sizing.node_size(_res(label=long, version=2),
                              False, tw, lambda n: False)[0]
        self.assertEqual(v2 - v1, sizing.BADGE_W)

    def test_expanded_thumbnail_grows_height_and_width(self):
        collapsed = sizing.node_size(_res(), False, tw, lambda n: False)
        expanded = sizing.node_size(_res(), False, tw, lambda n: True)
        self.assertEqual(expanded[1], collapsed[1] + sizing.THUMB_H + 8)
        self.assertGreaterEqual(expanded[0], sizing.THUMB_W + 2 * sizing.PAD)

    def test_buffer_ignores_expansion(self):
        a = sizing.node_size(_res(kind=RES_BUFFER), False, tw, lambda n: False)
        b = sizing.node_size(_res(kind=RES_BUFFER), False, tw, lambda n: True)
        self.assertEqual(a, b)

    def test_resource_bundle_height(self):
        members = ['r%d' % i for i in range(3)]
        _w, h = sizing.node_size(_res(members=members),
                                 False, tw, lambda n: False)
        self.assertEqual(h, sizing.TITLE_H + 3 * sizing.BUNDLE_ROW_H + 8.0)


class TestMeasureMemberRows(unittest.TestCase):
    def test_row_count_no_overflow(self):
        _w, rows = sizing.measure_member_rows(
            lambda nm: 50.0, ['a', 'b', 'c'], 100.0)
        self.assertEqual(rows, 3)

    def test_overflow_adds_one_row(self):
        members = ['m%d' % i for i in range(sizing.BUNDLE_MAX_ROWS + 5)]
        _w, rows = sizing.measure_member_rows(lambda nm: 10.0, members, 100.0)
        self.assertEqual(rows, sizing.BUNDLE_MAX_ROWS + 1)

    def test_min_width_clamp(self):
        w, _rows = sizing.measure_member_rows(lambda nm: 1.0, ['x'], 1.0)
        self.assertGreaterEqual(w, 170.0)


if __name__ == '__main__':
    unittest.main()
