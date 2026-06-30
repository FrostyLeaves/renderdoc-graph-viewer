# -*- coding: utf-8 -*-
"""Pure tooltip formatting extracted from GraphPanel. Runnable without PySide2."""
import unittest

from renderdoc_graph_viewer.ui import tooltip
from renderdoc_graph_viewer.graph_model import (
    NODE_PASS, NODE_RESOURCE, CAT_GRAPHICS, CAT_PORTAL, RES_COLOR, RES_BUFFER,
)


class _Edge(object):
    def __init__(self, kind, usages):
        self.kind = kind
        self.usages = usages


class _Pass(object):
    node_type = NODE_PASS

    def __init__(self, name='Pass', kind=CAT_GRAPHICS, first=1, last=3,
                 actions=2, marker_path=(), drillable=False, members=None,
                 portal_path=(), portal_focus_eid=None):
        self.name = name
        self.kind = kind
        self.first_eid = first
        self.last_eid = last
        self.action_count = actions
        self.marker_path = marker_path
        self.drillable = drillable
        self.bundle_members = members
        self.portal_path = portal_path
        self.portal_focus_eid = portal_focus_eid


class _Res(object):
    node_type = NODE_RESOURCE

    def __init__(self, name='RT', res_kind=RES_COLOR, version=1, writers=1,
                 readers=1, imported=False, info=None, members=None):
        self.name = name
        self.res_kind = res_kind
        self.version = version
        self.res_key = name
        self.writer_ids = list(range(writers))
        self.reader_ids = list(range(readers))
        self.imported = imported
        self.info = info or {}
        self.bundle_members = members


class TestEdgeTooltip(unittest.TestCase):
    def test_groups_and_truncates(self):
        usages = [(i, 'ColorTarget') for i in range(12)]
        t = tooltip.format_edge_tooltip(_Edge('write', usages))
        self.assertIn('[write]', t)
        self.assertIn('ColorTarget @ EID', t)
        self.assertIn('total', t)   # >10 EIDs collapses to a '+N total'


class TestNodeTooltip(unittest.TestCase):
    def test_pass_basic(self):
        t = tooltip.format_node_tooltip(_Pass(name='GBuffer'), {})
        self.assertIn('GBuffer', t)
        self.assertIn('actions: 2', t)

    def test_pass_drillable_hint(self):
        t = tooltip.format_node_tooltip(_Pass(drillable=True), {})
        self.assertIn('double-click to enter', t)

    def test_pass_bundle_listed(self):
        t = tooltip.format_node_tooltip(_Pass(members=['A', 'B', 'C']), {})
        self.assertIn('Bundle of 3', t)

    def test_portal_jump_hint(self):
        t = tooltip.format_node_tooltip(
            _Pass(kind=CAT_PORTAL, portal_path=('Post',)), {})
        self.assertIn('jump', t)

    def test_resource_version_line(self):
        t = tooltip.format_node_tooltip(_Res(name='HDR', version=2), {'HDR': 3})
        self.assertIn('#2 / 3', t)

    def test_resource_external_tag(self):
        t = tooltip.format_node_tooltip(_Res(imported=True), {})
        self.assertIn('external', t)

    def test_buffer_omits_eye_hint(self):
        t = tooltip.format_node_tooltip(_Res(res_kind=RES_BUFFER), {})
        self.assertNotIn('eye:', t)


if __name__ == '__main__':
    unittest.main()
