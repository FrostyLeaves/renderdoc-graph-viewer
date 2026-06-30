# -*- coding: utf-8 -*-
"""Graph layout adapter (layout.graph_layout.compute_layout): left-to-right edge
flow, determinism, no in-column overlap, ALAP placement, cycle tolerance with a
warning, and the deterministic backfill that covers every node when the engine
raises."""
import unittest

from renderdoc_graph_viewer.graph_model import build_passes, build_graph
from renderdoc_graph_viewer.layout import graph_layout
from tests.fakes import draw

RES_INFO = {'A': {'kind': 'color', 'info': {}}, 'B': {'kind': 'color', 'info': {}}}


def _fixture():
    passes = build_passes([
        draw(10, ('A',), markers=('P1',)),
        draw(20, ('B',), markers=('P2',)),
    ])
    fg = build_graph(passes, {
        'A': [(10, 'ColorTarget'), (20, 'PS_Resource')],
        'B': [(20, 'ColorTarget')],
    }, RES_INFO)
    return fg


def _sizes(fg):
    return dict((n.id, (120.0, 50.0)) for n in fg.nodes())


class TestLayout(unittest.TestCase):
    def test_edges_flow_left_to_right(self):
        fg = _fixture()
        pos, warns = graph_layout.compute_layout(fg.nodes(), fg.edges, _sizes(fg))
        self.assertEqual(warns, [])
        self.assertEqual(len(pos), len(fg.nodes()))
        for e in fg.edges:
            self.assertLess(pos[e.src_id][0], pos[e.dst_id][0],
                            'edge %s->%s must flow rightward' % (e.src_id, e.dst_id))

    def test_deterministic(self):
        fg = _fixture()
        p1, _ = graph_layout.compute_layout(fg.nodes(), fg.edges, _sizes(fg))
        p2, _ = graph_layout.compute_layout(fg.nodes(), fg.edges, _sizes(fg))
        self.assertEqual(p1, p2)

    def test_no_overlap_within_column(self):
        fg = _fixture()
        pos, _ = graph_layout.compute_layout(fg.nodes(), fg.edges, _sizes(fg))
        cols = {}
        for nid, (x, y) in pos.items():
            cols.setdefault(x, []).append(y)
        for ys in cols.values():
            ys = sorted(ys)
            for a, b in zip(ys, ys[1:]):
                self.assertGreaterEqual(b - a, 50.0)

    def test_cycle_falls_back_with_warning(self):
        fg = _fixture()

        class FakeEdge(object):
            def __init__(self, s, d):
                self.src_id = s
                self.dst_id = d
                self.kind = 'read'

        edges = list(fg.edges) + [FakeEdge(fg.passes[1].id, fg.passes[0].id),
                                  FakeEdge(fg.passes[0].id, fg.passes[1].id)]
        pos, warns = graph_layout.compute_layout(fg.nodes(), edges, _sizes(fg))
        self.assertEqual(len(warns), 1)
        self.assertEqual(len(pos), len(fg.nodes()))

    def test_empty_graph(self):
        pos, warns = graph_layout.compute_layout([], [], {})
        self.assertEqual(pos, {})
        self.assertEqual(warns, [])

    def test_two_parallel_chains_keep_vertical_gap(self):
        passes = build_passes([
            draw(10, ('A',), markers=('P1',)),
            draw(20, ('B',), markers=('P2',)),
        ])
        fg = build_graph(passes, {
            'A': [(10, 'ColorTarget')],
            'B': [(20, 'ColorTarget')],
        }, RES_INFO)
        sizes = _sizes(fg)
        pos, _ = graph_layout.compute_layout(fg.nodes(), fg.edges, sizes,
                                             rank_edges=fg.rank_edges)
        ys = sorted(pos[p.id][1] for p in fg.passes)
        if abs(pos[fg.passes[0].id][0] - pos[fg.passes[1].id][0]) < 1.0:
            # same-column vertical separation
            self.assertGreaterEqual(ys[1] - ys[0], 50.0)

    def test_mid_nodes_also_pull_toward_consumers(self):
        # ALAP layering: any node whose consumer sits late moves next to it
        # instead of staying at the earliest feasible column
        nodes = [_FakeNode('a', 0), _FakeNode('b', 1),
                 _FakeNode('c0', 2), _FakeNode('c1', 3),
                 _FakeNode('c2', 4), _FakeNode('c3', 5)]
        edges = [_FakeEdge('a', 'b'),
                 _FakeEdge('c0', 'c1'), _FakeEdge('c1', 'c2'),
                 _FakeEdge('c2', 'c3'),
                 _FakeEdge('b', 'c3', 'late')]
        sizes = dict((n.id, (100.0, 40.0)) for n in nodes)
        pos, warns = graph_layout.compute_layout(nodes, edges, sizes)
        self.assertEqual(warns, [])
        # b hugs its only consumer c3 (sits just left of it), a follows suit;
        # neither is stranded at the far-left chain head
        self.assertGreater(pos['b'][0], pos['c0'][0])
        self.assertLess(pos['b'][0], pos['c3'][0])
        self.assertGreater(pos['a'][0], pos['c0'][0])
        self.assertLess(pos['a'][0], pos['b'][0])

    def test_sources_pull_toward_their_consumers(self):
        # input consumed only by a late pass
        nodes = [_FakeNode('a', 0), _FakeNode('b', 1), _FakeNode('c', 2),
                 _FakeNode('d', 3), _FakeNode('ext')]
        edges = [_FakeEdge('a', 'b'), _FakeEdge('b', 'c'),
                 _FakeEdge('c', 'd'), _FakeEdge('ext', 'd', 'read')]
        sizes = dict((n.id, (100.0, 40.0)) for n in nodes)
        pos, warns = graph_layout.compute_layout(nodes, edges, sizes)
        self.assertEqual(warns, [])
        self.assertGreater(pos['ext'][0], pos['b'][0])
        self.assertLess(pos['ext'][0], pos['d'][0])
        # genuine chain heads keep their natural leftmost position
        self.assertEqual(pos['a'][0], min(p[0] for p in pos.values()))

    def test_rank_edges_parameter_controls_ranking(self):
        # drawn edges may legitimately contain cycles (e.g. write-backs in
        # merged mode); rank_edges provides the acyclic ranking set
        nodes = [_FakeNode('a', 0), _FakeNode('b', 1)]
        edges = [_FakeEdge('a', 'b', 'write'), _FakeEdge('b', 'a', 'read')]
        sizes = dict((n.id, (120.0, 50.0)) for n in nodes)

        # without rank_edges the drawn 2-cycle trips the cycle warning
        _pos, warns = graph_layout.compute_layout(nodes, edges, sizes)
        self.assertEqual(len(warns), 1)

        # with rank_edges: clean ranks following the DAG subset
        pos, warns = graph_layout.compute_layout(
            nodes, edges, sizes, rank_edges=[_FakeEdge('a', 'b', 'write')])
        self.assertEqual(warns, [])
        self.assertLess(pos['a'][0], pos['b'][0])


class _FakeNode(object):
    def __init__(self, nid, order=None):
        self.id = nid
        if order is not None:
            self.order = order


class _FakeEdge(object):
    def __init__(self, s, d, kind='read'):
        self.src_id = s
        self.dst_id = d
        self.kind = kind


class _N(object):
    def __init__(self, nid, order):
        self.id = nid
        self.order = order


class _E(object):
    def __init__(self, s, d, kind='write'):
        self.src_id = s
        self.dst_id = d
        self.kind = kind


def _chain(n):
    nodes = [_N('n%d' % i, i) for i in range(n)]
    edges = [_E('n%d' % i, 'n%d' % (i + 1)) for i in range(n - 1)]
    sizes = dict((x.id, (120.0, 50.0)) for x in nodes)
    return nodes, edges, sizes


class TestEngineChain(unittest.TestCase):
    """GKNV is the only layout engine; a deterministic backfill covers any
    node the engine fails to place (defensive — only on engine exception)."""

    def test_layout_covers_all_nodes(self):
        nodes, edges, sizes = _chain(4)
        positions, warns = graph_layout.compute_layout(nodes, edges, sizes)
        self.assertEqual(sorted(positions.keys()),
                         sorted(n.id for n in nodes))
        self.assertEqual(warns, [])           # healthy: no warnings

    def test_engine_failure_backfills_all_nodes(self):
        nodes, edges, sizes = _chain(4)
        orig_g = graph_layout.compute_layout_gknv
        graph_layout.compute_layout_gknv = _boom
        try:
            positions, warns = graph_layout.compute_layout(nodes, edges, sizes)
            self.assertEqual(sorted(positions.keys()),
                             sorted(n.id for n in nodes))  # backfill covered all
            self.assertTrue(warns)  # a warning was emitted
        finally:
            graph_layout.compute_layout_gknv = orig_g


def _boom(*a, **k):
    raise RuntimeError('forced failure')


if __name__ == '__main__':
    unittest.main()
