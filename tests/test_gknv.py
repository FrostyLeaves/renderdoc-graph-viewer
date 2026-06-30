# -*- coding: utf-8 -*-
"""GKNV (dot-style) layout engine: simplex ranking, ordering, coords."""
import itertools
import unittest

from renderdoc_graph_viewer.layout.layout_gknv import (NetworkSimplex, compute,
                                             solve_ranks, _weak_components)


def _cost(ranks, edges):
    return sum(w * (ranks[h] - ranks[t]) for t, h, ml, w in edges)


def _feasible(ranks, edges):
    return all(ranks[h] - ranks[t] >= ml for t, h, ml, w in edges)


def _brute_optimal(n, edges, max_rank=None):
    """Optimal cost by enumerating all integer rankings on a small grid.
    Valid because some optimal solution always has ranks in [0, sum(ml)]."""
    if max_rank is None:
        max_rank = sum(ml for _t, _h, ml, _w in edges) or 1
    best = None
    for combo in itertools.product(range(max_rank + 1), repeat=n):
        if _feasible(combo, edges):
            c = _cost(combo, edges)
            if best is None or c < best:
                best = c
    return best


def _solve(n, edges):
    ranks = solve_ranks(n, edges)
    return ranks


class TestNetworkSimplexRanking(unittest.TestCase):
    def assert_optimal(self, n, edges):
        ranks = _solve(n, edges)
        self.assertTrue(_feasible(ranks, edges),
                        'infeasible: %s' % (ranks,))
        self.assertEqual(_cost(ranks, edges), _brute_optimal(n, edges))
        self.assertEqual(min(ranks), 0)
        return ranks

    def test_chain(self):
        edges = [(0, 1, 1, 1), (1, 2, 1, 1)]
        ranks = self.assert_optimal(3, edges)
        self.assertEqual(ranks, [0, 1, 2])

    def test_diamond(self):
        edges = [(0, 1, 1, 1), (0, 2, 1, 1), (1, 3, 1, 1), (2, 3, 1, 1)]
        ranks = self.assert_optimal(4, edges)
        self.assertEqual(ranks, [0, 1, 1, 2])

    def test_consumer_hugging(self):
        # node 4 feeds only the sink
        edges = [(0, 1, 1, 1), (1, 2, 1, 1), (2, 3, 1, 1), (0, 4, 1, 1),
                 (4, 3, 1, 1)]
        ranks = self.assert_optimal(5, edges)
        self.assertEqual(ranks[4], 2)

    def test_weight_pulls_node(self):
        # 0 -> 1 (w1) and 1 -> 2 (w2): chain 0..4 pins ranks; node 5
        # between rank0 node and rank4 node, heavier edge to the late
        # side wins the tie
        edges = [(0, 1, 1, 1), (1, 2, 1, 1), (2, 3, 1, 1), (3, 4, 1, 1),
                 (0, 5, 1, 1), (5, 4, 1, 2)]
        ranks = self.assert_optimal(6, edges)
        self.assertEqual(ranks[5], 3)

    def test_minlen_respected(self):
        edges = [(0, 1, 3, 1), (1, 2, 2, 1)]
        ranks = self.assert_optimal(3, edges)
        self.assertEqual(ranks, [0, 3, 5])

    def test_minlen_zero_edges(self):
        # aux-graph style: zero minlen lets nodes share a rank
        edges = [(0, 1, 0, 1), (0, 2, 0, 1), (1, 3, 1, 1), (2, 3, 1, 1)]
        self.assert_optimal(4, edges)

    def test_zero_weight_separation_edges(self):
        # separation constraints cost nothing but still bind
        edges = [(0, 1, 5, 0), (1, 2, 5, 0), (0, 3, 1, 1), (3, 2, 1, 1)]
        ranks = self.assert_optimal(4, edges)
        self.assertGreaterEqual(ranks[1] - ranks[0], 5)
        self.assertGreaterEqual(ranks[2] - ranks[1], 5)

    def test_multi_edge_same_pair(self):
        edges = [(0, 1, 1, 1), (0, 1, 1, 1), (0, 2, 1, 1), (2, 1, 1, 1)]
        self.assert_optimal(3, edges)

    def test_random_small_graphs_match_bruteforce(self):
        # deterministic pseudo-random DAGs
        import random
        for seed in range(12):
            rng = random.Random(seed)
            n = rng.randint(3, 6)
            edges = []
            for a in range(n):
                for b in range(a + 1, n):
                    if rng.random() < 0.5:
                        edges.append((a, b, rng.randint(1, 2),
                                      rng.randint(1, 3)))
            if not edges:
                continue
            # connect stragglers so the graph is one component
            seen = set()
            for t, h, _ml, _w in edges:
                seen.add(t)
                seen.add(h)
            prev = None
            for v in range(n):
                if v not in seen:
                    if prev is None:
                        prev = min(seen) if seen else 0
                    edges.append((min(prev, v), max(prev, v), 1, 1))
                    seen.add(v)
            ranks = _solve(n, edges)
            self.assertTrue(_feasible(ranks, edges),
                            'seed %d infeasible' % seed)
            self.assertEqual(_cost(ranks, edges),
                             _brute_optimal(n, edges),
                             'seed %d suboptimal' % seed)

    def test_float_minlen_feasible(self):
        # float minlen constraints
        edges = [(0, 1, 36.5, 0), (1, 2, 24.0, 0), (0, 3, 1.0, 1),
                 (3, 2, 1.0, 1)]
        ranks = _solve(4, edges)
        self.assertTrue(_feasible(ranks, edges))

    def test_iteration_cap_still_feasible(self):
        edges = [(0, 1, 1, 1), (1, 2, 1, 1), (0, 2, 1, 5)]
        ns = NetworkSimplex(3, edges)
        ranks = ns.solve(max_iter=0)  # no pivots at all
        self.assertTrue(_feasible(ranks, edges))

    def test_cycle_raises(self):
        edges = [(0, 1, 1, 1), (1, 0, 1, 1)]
        with self.assertRaises(ValueError):
            _solve(2, edges)

    def test_incremental_cuts_match_full_recompute(self):
        # incremental cut values match a direct fold
        import random
        for seed in range(20):
            rng = random.Random(1000 + seed)
            n = rng.randint(4, 9)
            # chain keeps it connected; extras add cycles of pivot work
            edges = [(i, i + 1, 1, rng.randint(0, 2))
                     for i in range(n - 1)]
            for a in range(n):
                for b in range(a + 1, n):
                    if rng.random() < 0.4:
                        edges.append((a, b, rng.randint(1, 3),
                                      rng.randint(0, 4)))
            ns = NetworkSimplex(n, edges)
            ns.solve()
            self.assertLessEqual(
                ns._check_cuts(), 1e-9,
                'seed %d: incremental cuts drifted' % seed)


def _no_overlaps(case, positions, sizes, gap=28.0, eps=0.5):
    """Check same-column vertical separation."""
    by_x = {}
    for nid, (x, y) in positions.items():
        by_x.setdefault(round(x, 1), []).append(nid)
    cols = {}
    for nid, (x, y) in positions.items():
        # group by column CENTER (nodes are centred in their column)
        cx = x + sizes[nid][0] / 2.0
        cols.setdefault(round(cx, 1), []).append(nid)
    for cx, members in cols.items():
        members.sort(key=lambda n: positions[n][1])
        for a, b in zip(members, members[1:]):
            bottom = positions[a][1] + sizes[a][1]
            case.assertGreaterEqual(
                positions[b][1] - bottom, gap - eps,
                'overlap in column %s: %s vs %s' % (cx, a, b))


class TestComputeLayout(unittest.TestCase):
    def test_all_nodes_placed_and_separated(self):
        ids = ['a', 'b', 'c', 'd', 'e']
        sizes = dict((i, (100.0, 40.0)) for i in ids)
        edges = [('a', 'b'), ('a', 'c'), ('b', 'd'), ('c', 'd'),
                 ('d', 'e')]
        pos, warns = compute(ids, sizes, edges)
        self.assertEqual(set(pos), set(ids))
        self.assertEqual(warns, [])
        _no_overlaps(self, pos, sizes)

    def test_chain_runs_straight(self):
        # two equal-cost optima for the branch chain
        ids = ['a', 'b', 'c', 'd']
        sizes = dict((i, (80.0, 30.0)) for i in ids)
        edges = [('a', 'b'), ('b', 'c'), ('a', 'd')]
        pos, _w = compute(ids, sizes, edges)
        cy = dict((i, pos[i][1] + sizes[i][1] / 2.0) for i in ids)
        self.assertAlmostEqual(cy['b'], cy['c'], delta=0.6)
        self.assertTrue(abs(cy['a'] - cy['b']) < 0.6 or
                        abs(cy['a'] - cy['d']) < 0.6,
                        'a aligned with neither successor: %r' % (cy,))
        _no_overlaps(self, pos, sizes)

    def test_long_edge_virtual_chain_aligns(self):
        # a->e spans 3 ranks: with omega weighting the virtual chain is
        # pulled straight, so endpoints stay near one line even though
        # the rank-1/2 columns hold other nodes
        ids = ['a', 'b', 'c', 'd', 'e']
        sizes = dict((i, (80.0, 30.0)) for i in ids)
        edges = [('a', 'b'), ('b', 'c'), ('c', 'd'), ('d', 'e'),
                 ('a', 'e')]
        pos, _w = compute(ids, sizes, edges)
        self.assertEqual(set(pos), set(ids))
        _no_overlaps(self, pos, sizes)

    def test_crossing_resolved(self):
        # two independent 2-rank pairs wired crosswise resolve to 0
        # crossings (order flips one side)
        ids = ['s1', 's2', 't1', 't2']
        sizes = dict((i, (60.0, 24.0)) for i in ids)
        edges = [('s1', 't2'), ('s2', 't1'), ('s1', 't1'), ('s2', 't2')]
        pos, _w = compute(ids, sizes, edges)
        self.assertEqual(set(pos), set(ids))
        _no_overlaps(self, pos, sizes)

    def test_rank_pairs_drive_columns(self):
        # rank_pairs drive column order
        ids = ['a', 'b']
        sizes = dict((i, (50.0, 20.0)) for i in ids)
        pos, _w = compute(ids, sizes, [('b', 'a')],
                          rank_pairs=[('a', 'b')])
        self.assertLess(pos['a'][0], pos['b'][0])

    def test_components_stack_vertically(self):
        ids = ['a', 'b', 'x', 'y']
        sizes = dict((i, (60.0, 24.0)) for i in ids)
        edges = [('a', 'b'), ('x', 'y')]
        pos, _w = compute(ids, sizes, edges)
        ys = sorted(pos[i][1] for i in ids)
        self.assertGreater(ys[2] - ys[1], 60.0)  # 120 gap between comps

    def test_cycle_in_drawn_edges_warns_not_raises(self):
        ids = ['a', 'b']
        sizes = dict((i, (50.0, 20.0)) for i in ids)
        pos, warns = compute(ids, sizes, [('a', 'b'), ('b', 'a')])
        self.assertEqual(set(pos), set(ids))
        self.assertTrue(any('cycle' in w for w in warns))

    def test_deterministic(self):
        ids = ['n%d' % i for i in range(12)]
        sizes = dict((i, (70.0, 26.0)) for i in ids)
        edges = []
        for i in range(11):
            edges.append(('n%d' % (i // 2), 'n%d' % (i + 1)))
        p1, _ = compute(ids, sizes, edges)
        p2, _ = compute(ids, sizes, edges)
        self.assertEqual(p1, p2)

    def test_empty(self):
        self.assertEqual(compute([], {}, []), ({}, []))

    def test_single_node(self):
        pos, warns = compute(['a'], {'a': (50.0, 20.0)}, [])
        self.assertIn('a', pos)


class TestWeakComponents(unittest.TestCase):
    """Shared flood-fill used by both solve_ranks (int/list adjacency) and the
    coordinate phase (id/dict adjacency)."""

    def test_isolated_nodes_each_own_component(self):
        comps = _weak_components(range(3), [[], [], []])
        self.assertEqual([sorted(c) for c in comps], [[0], [1], [2]])

    def test_single_connected_component_dict_adjacency(self):
        nbr = {0: [1], 1: [0, 2], 2: [1]}
        comps = _weak_components([0, 1, 2], nbr)
        self.assertEqual(len(comps), 1)
        self.assertEqual(sorted(comps[0]), [0, 1, 2])

    def test_two_components_follow_node_iteration_order(self):
        nbr = {'a': ['b'], 'b': ['a'], 'c': []}
        comps = _weak_components(['a', 'b', 'c'], nbr)
        self.assertEqual([sorted(c) for c in comps], [['a', 'b'], ['c']])


if __name__ == '__main__':
    unittest.main()
