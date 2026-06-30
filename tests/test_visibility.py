# -*- coding: utf-8 -*-
"""Pure widget logic extracted from GraphPanel: display filtering, selection
closure, preview tri-state. Runnable without PySide2."""
import unittest

from renderdoc_graph_viewer.ui import visibility
from renderdoc_graph_viewer import config
from renderdoc_graph_viewer.graph_model import CAT_PORTAL, CAT_GRAPHICS


class _P(object):
    def __init__(self, nid, kind=CAT_GRAPHICS):
        self.id = nid
        self.kind = kind


class _R(object):
    def __init__(self, nid, imported=False, internal=False, scope_input=False):
        self.id = nid
        self.imported = imported
        self.internal = internal
        self.scope_input = scope_input


class _G(object):
    def __init__(self, passes, resources, orphans=()):
        self.passes = passes
        self.resources = resources
        self.orphan_pass_ids = set(orphans)


def _display(**over):
    d = config.display_of(config.DEFAULTS)
    d.update(over)
    return d


class TestFilterVisible(unittest.TestCase):
    def test_orphans_hidden_when_disabled(self):
        g = _G([_P('p0'), _P('p1')], [], orphans=['p1'])
        vp, _vr, counts = visibility.filter_visible(
            g, _display(**{config.KEY_SHOW_ORPHANS: False}))
        self.assertEqual([p.id for p in vp], ['p0'])
        self.assertEqual(counts['orphans'], 1)

    def test_orphans_shown_when_enabled(self):
        g = _G([_P('p0'), _P('p1')], [], orphans=['p1'])
        vp, _vr, counts = visibility.filter_visible(
            g, _display(**{config.KEY_SHOW_ORPHANS: True}))
        self.assertEqual(len(vp), 2)
        self.assertEqual(counts['orphans'], 0)

    def test_portals_hidden_when_disabled(self):
        g = _G([_P('p0'), _P('pt', CAT_PORTAL)], [])
        vp, _vr, _c = visibility.filter_visible(g, _display(**{
            config.KEY_SHOW_PORTALS: False, config.KEY_SHOW_ORPHANS: True}))
        self.assertEqual([p.id for p in vp], ['p0'])

    def test_external_and_internal_hidden(self):
        res = [_R('ext', imported=True), _R('int', internal=True), _R('keep')]
        g = _G([], res)
        _vp, vr, counts = visibility.filter_visible(g, _display(**{
            config.KEY_SHOW_EXTERNAL: False, config.KEY_SHOW_INTERNAL: False}))
        self.assertEqual([r.id for r in vr], ['keep'])
        self.assertEqual(counts['external'], 1)
        self.assertEqual(counts['internal'], 1)

    def test_scope_input_kept_despite_imported(self):
        g = _G([], [_R('si', imported=True, scope_input=True)])
        _vp, vr, counts = visibility.filter_visible(
            g, _display(**{config.KEY_SHOW_EXTERNAL: False}))
        self.assertEqual([r.id for r in vr], ['si'])
        self.assertEqual(counts['external'], 0)


class TestClosure(unittest.TestCase):
    def test_forward_and_backward(self):
        edges = [('a', 'b'), ('b', 'c'), ('x', 'b')]
        self.assertEqual(visibility.closure_of('b', edges),
                         set(['a', 'b', 'c', 'x']))

    def test_isolated_start(self):
        self.assertEqual(visibility.closure_of('z', [('a', 'b')]),
                         set(['z']))


class TestCycleExpanded(unittest.TestCase):
    def test_tristate_cycle(self):
        exp = {}
        self.assertTrue(visibility.cycle_expanded(exp, 'k'))   # -> raw
        self.assertEqual(exp, {'k': False})
        self.assertTrue(visibility.cycle_expanded(exp, 'k'))   # -> fitted
        self.assertEqual(exp, {'k': True})
        self.assertFalse(visibility.cycle_expanded(exp, 'k'))  # -> collapsed
        self.assertEqual(exp, {})


def _vn(nid, is_pass=False, label='', res_key=None):
    return (nid, is_pass, label, res_key)


class TestVisualState(unittest.TestCase):
    def test_no_selection_no_filter_all_normal(self):
        nodes = [_vn('a', True, 'A'), _vn('b', False, 'B', 'b')]
        ns, es = visibility.visual_state(nodes, [(0, 'a', 'b')], None, '')
        self.assertEqual(ns['a']['opacity'], 1.0)
        self.assertEqual(ns['a']['z'], visibility.Z_NODE_BASE)
        self.assertFalse(ns['a']['selected'])
        self.assertEqual(es[0], {'emphasis': 'normal', 'opacity': 1.0})

    def test_filter_dims_non_matching(self):
        nodes = [_vn('a', True, 'Shadow'), _vn('b', True, 'GBuffer')]
        ns, _ = visibility.visual_state(nodes, [], None, 'shadow')
        self.assertEqual(ns['a']['opacity'], 1.0)
        self.assertEqual(ns['b']['opacity'], visibility.FILTER_DIM_OPACITY)

    def test_selection_closure_z_and_edge_emphasis(self):
        # a -> b -> c chain; d unrelated. Select a.
        nodes = [_vn('a', True, 'A'), _vn('b', False, 'B', 'b'),
                 _vn('c', True, 'C'), _vn('d', True, 'D')]
        edges = [(0, 'a', 'b'), (1, 'b', 'c'), (2, 'd', 'd')]
        ns, es = visibility.visual_state(nodes, edges, 'a', '')
        self.assertEqual(ns['a']['z'], visibility.Z_NODE_HI)
        self.assertTrue(ns['a']['selected'])
        self.assertEqual(ns['d']['opacity'], visibility.DIM_OPACITY)
        self.assertEqual(ns['d']['z'], visibility.Z_NODE_BASE)
        self.assertEqual(es[0]['emphasis'], 'direct')      # touches selection
        self.assertEqual(es[1]['emphasis'], 'indirect')    # inside closure
        self.assertEqual(es[2]['emphasis'], 'muted')       # outside closure

    def test_resource_twins_select_together(self):
        nodes = [_vn('r1', False, 'Tex', 'X'), _vn('r2', False, 'Tex', 'X'),
                 _vn('r3', False, 'Other', 'Y')]
        ns, _ = visibility.visual_state(nodes, [], 'r1', '')
        self.assertTrue(ns['r1']['selected'])
        self.assertTrue(ns['r2']['selected'])     # same res_key
        self.assertFalse(ns['r3']['selected'])

    def test_edge_with_missing_node_is_omitted(self):
        ns, es = visibility.visual_state([_vn('a', True, 'A')],
                                         [(0, 'a', 'gone')], None, '')
        self.assertNotIn(0, es)


class TestNextMatch(unittest.TestCase):
    def test_empty_matches(self):
        self.assertEqual(visibility.next_match([], 'x', '', -1), (-1, None))

    def test_first_match_sorts_left_to_right(self):
        m = [(10.0, 0.0, 'a'), (0.0, 0.0, 'b')]   # b is left of a
        self.assertEqual(visibility.next_match(m, 'q', '', -1), (0, 'b'))

    def test_cycle_advances_and_wraps(self):
        m = [(0.0, 0.0, 'a'), (1.0, 0.0, 'b')]
        self.assertEqual(visibility.next_match(m, 'q', 'q', 0), (1, 'b'))
        self.assertEqual(visibility.next_match(m, 'q', 'q', 1), (0, 'a'))  # wrap

    def test_text_change_restarts_at_zero(self):
        m = [(0.0, 0.0, 'a'), (1.0, 0.0, 'b')]
        self.assertEqual(visibility.next_match(m, 'new', 'old', 1), (0, 'a'))


if __name__ == '__main__':
    unittest.main()
