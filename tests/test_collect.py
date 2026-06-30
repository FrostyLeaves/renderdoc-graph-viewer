# -*- coding: utf-8 -*-
"""Action-tree flattening (graph_model._collect_leaves): which markers enter the
path (semantic kept; structural / API-call / renderpass-region excluded but
recursed), MultiAction stays one leaf, and per-API Present source-hint
resolution."""
import unittest

from renderdoc_graph_viewer import graph_model as gm
from tests.fakes import FakeRD, FakeAction

AF = FakeRD.ActionFlags
KEY = lambda v: v  # noqa: E731


def collect(roots):
    return gm._collect_leaves(FakeRD, roots, None, KEY)[0]


class TestCollectLeaves(unittest.TestCase):
    def test_semantic_marker_kept_in_path(self):
        d = FakeAction(10, AF.Drawcall, outputs=['A'])
        shadow = FakeAction(2, AF.PushMarker, name='Shadow', children=[d])
        leaves = collect([shadow])
        self.assertEqual(len(leaves), 1)
        self.assertEqual(leaves[0].marker_path, ('Shadow',))

    def test_cmdlist_flagged_marker_excluded_from_path(self):
        d = FakeAction(10, AF.Drawcall, outputs=['A'])
        shadow = FakeAction(3, AF.PushMarker, name='Shadow', children=[d])
        execu = FakeAction(1, AF.PushMarker | AF.CmdList,
                           name='vkCmdExecuteCommands(2)', children=[shadow])
        leaves = collect([execu])
        self.assertEqual(len(leaves), 1)
        self.assertEqual(leaves[0].marker_path, ('Shadow',))

    def test_api_call_named_marker_excluded_by_name(self):
        d = FakeAction(10, AF.Drawcall, outputs=['A'])
        execu = FakeAction(1, AF.PushMarker,
                           name='vkCmdExecuteCommands(2)', children=[d])
        leaves = collect([execu])
        self.assertEqual(leaves[0].marker_path, ())

    def test_renderpass_region_excluded_from_path(self):
        d = FakeAction(10, AF.Drawcall, outputs=['A'])
        rp = FakeAction(1, AF.PushMarker | AF.BeginPass | AF.PassBoundary,
                        name='Colour Pass #1 (1 Targets + Depth)', children=[d])
        leaves = collect([rp])
        self.assertEqual(leaves[0].marker_path, ())

    def test_queue_submit_wrapper_recursed_without_naming(self):
        d = FakeAction(10, AF.Drawcall, outputs=['A'])
        submit = FakeAction(1, AF.PushMarker, name='vkQueueSubmit(1)',
                            children=[d])
        leaves = collect([submit])
        self.assertEqual(len(leaves), 1)
        self.assertEqual(leaves[0].marker_path, ())

    def test_multiaction_stays_single_leaf(self):
        sub = FakeAction(11, AF.Drawcall)
        multi = FakeAction(10, AF.Drawcall | AF.MultiAction,
                           name='MultiDrawIndirect(8)', children=[sub],
                           outputs=['A'])
        leaves = collect([multi])
        self.assertEqual(len(leaves), 1)
        self.assertEqual(leaves[0].eid, 10)

    def test_plain_grouping_node_recursed(self):
        d = FakeAction(10, AF.Dispatch)
        grp = FakeAction(1, 0, name='Command Buffer 0xDEAD', children=[d])
        leaves = collect([grp])
        self.assertEqual(len(leaves), 1)
        self.assertEqual(leaves[0].kind, 'dispatch')
        self.assertEqual(leaves[0].marker_path, ())

    def test_present_backbuffer_taken_from_copy_destination(self):
        # Vulkan vkQueuePresentKHR can expose the backbuffer via copyDestination.
        pres = FakeAction(20, AF.Present, name='vkQueuePresentKHR(SC)',
                          copySource=None, copyDestination='SC')
        leaves = collect([pres])
        self.assertEqual(len(leaves), 1)
        self.assertEqual(leaves[0].kind, 'present')
        self.assertEqual(leaves[0].copy_src_hint, 'SC')

    def test_present_keeps_copy_source_when_set(self):
        # copySource path
        pres = FakeAction(20, AF.Present, name='Present',
                          copySource='SC', copyDestination=None)
        leaves = collect([pres])
        self.assertEqual(leaves[0].copy_src_hint, 'SC')


if __name__ == '__main__':
    unittest.main()
