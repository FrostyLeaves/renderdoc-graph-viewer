# -*- coding: utf-8 -*-
"""USAGE_ACCESS taxonomy: each ResourceUsage name maps to the right
read/write/rw/ignore class, and LeafAction.targets() unions its outputs."""
import unittest

from renderdoc_graph_viewer import graph_model as gm


class TestUsageClassification(unittest.TestCase):
    def test_stage_resources_are_reads(self):
        for s in ('VS', 'HS', 'DS', 'GS', 'PS', 'CS', 'TS', 'MS', 'All'):
            self.assertEqual(gm.USAGE_ACCESS['%s_Resource' % s], gm.READ)
            self.assertEqual(gm.USAGE_ACCESS['%s_Constants' % s], gm.READ)
            self.assertEqual(gm.USAGE_ACCESS['%s_RWResource' % s], gm.RW)

    def test_write_usages(self):
        for n in ('ColorTarget', 'DepthStencilTarget', 'Clear', 'Discard',
                  'CopyDst', 'ResolveDst', 'StreamOut'):
            self.assertEqual(gm.USAGE_ACCESS[n], gm.WRITE)

    def test_read_usages(self):
        for n in ('VertexBuffer', 'IndexBuffer', 'InputTarget', 'Indirect',
                  'CopySrc', 'ResolveSrc', 'Present'):
            self.assertEqual(gm.USAGE_ACCESS[n], gm.READ)

    def test_rw_usages(self):
        for n in ('Copy', 'Resolve', 'GenMips'):
            self.assertEqual(gm.USAGE_ACCESS[n], gm.RW)

    def test_ignored_usages(self):
        for n in ('Barrier', 'Unused', 'CPUWrite'):
            self.assertEqual(gm.USAGE_ACCESS[n], gm.IGNORE)

    def test_leaf_action_targets(self):
        a = gm.LeafAction(5, 'clear', group_outputs=('t1',),
                          group_depth='t2', copy_dst_hint='t3')
        self.assertEqual(a.targets(), {'t1', 't2', 't3'})


if __name__ == '__main__':
    unittest.main()
