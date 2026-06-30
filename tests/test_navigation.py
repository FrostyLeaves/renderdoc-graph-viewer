# -*- coding: utf-8 -*-
"""Navigation state model: the scope stack + bounded back-history the controller
used to own inline inside the qrenderdoc-gated window. Pure, no PySide2/renderdoc
- view-state snapshots are opaque values passed straight through."""
import unittest

from renderdoc_graph_viewer.navigation import NavigationState, NAV_HISTORY_LIMIT


class _Node(object):
    """Minimal drillable-node duck type: the fields drill() reads."""

    def __init__(self, name, marker_path, first_eid, last_eid):
        self.name = name
        self.marker_path = marker_path
        self.first_eid = first_eid
        self.last_eid = last_eid


class TestRootState(unittest.TestCase):
    def test_empty_is_whole_frame_root(self):
        nav = NavigationState()
        self.assertEqual(nav.current_scope(), ((), None))
        self.assertEqual(nav.labels(), [])
        self.assertFalse(nav.can_back)


class TestDrill(unittest.TestCase):
    def test_drill_appends_scope_from_node_fields(self):
        nav = NavigationState()
        nav.drill(_Node('Shadows', ('Shadows',), 10, 25), view_state='V0')
        self.assertEqual(nav.current_scope(), (('Shadows',), (10, 25)))
        self.assertEqual(nav.labels(), ['Shadows'])

    def test_drill_coerces_marker_path_to_tuple(self):
        nav = NavigationState()
        nav.drill(_Node('GBuffer', ['Frame', 'GBuffer'], 3, 8), view_state=None)
        path, rng = nav.current_scope()
        self.assertEqual(path, ('Frame', 'GBuffer'))
        self.assertIsInstance(path, tuple)
        self.assertEqual(rng, (3, 8))

    def test_drill_enables_back(self):
        nav = NavigationState()
        self.assertFalse(nav.can_back)
        nav.drill(_Node('A', ('A',), 1, 2), view_state='V')
        self.assertTrue(nav.can_back)

    def test_nested_drill_stacks_labels(self):
        nav = NavigationState()
        nav.drill(_Node('A', ('A',), 1, 9), view_state='V0')
        nav.drill(_Node('B', ('A', 'B'), 2, 5), view_state='V1')
        self.assertEqual(nav.labels(), ['A', 'B'])
        self.assertEqual(nav.current_scope(), (('A', 'B'), (2, 5)))


class TestBack(unittest.TestCase):
    def test_back_restores_previous_scope_and_returns_view(self):
        nav = NavigationState()
        nav.drill(_Node('A', ('A',), 1, 9), view_state='V_root')
        nav.drill(_Node('B', ('A', 'B'), 2, 5), view_state='V_A')
        # popping returns the viewpoint captured when we left that view
        self.assertEqual(nav.back(), 'V_A')
        self.assertEqual(nav.current_scope(), (('A',), (1, 9)))
        self.assertEqual(nav.back(), 'V_root')
        self.assertEqual(nav.current_scope(), ((), None))

    def test_back_returns_stored_none_view(self):
        # None view-state snapshot
        nav = NavigationState()
        nav.drill(_Node('A', ('A',), 1, 9), view_state=None)
        self.assertTrue(nav.can_back)
        self.assertIsNone(nav.back())
        self.assertFalse(nav.can_back)

    def test_can_back_false_after_draining_history(self):
        nav = NavigationState()
        nav.drill(_Node('A', ('A',), 1, 9), view_state='V')
        nav.back()
        self.assertFalse(nav.can_back)


class TestNavigate(unittest.TestCase):
    def test_navigate_truncates_to_index(self):
        nav = NavigationState()
        nav.drill(_Node('A', ('A',), 1, 9), view_state='V0')
        nav.drill(_Node('B', ('A', 'B'), 2, 8), view_state='V1')
        nav.drill(_Node('C', ('A', 'B', 'C'), 3, 4), view_state='V2')
        # breadcrumb index 1 keeps only the first scope (root is index 0)
        nav.navigate(1, view_state='V3')
        self.assertEqual(nav.labels(), ['A'])
        self.assertEqual(nav.current_scope(), (('A',), (1, 9)))

    def test_navigate_zero_returns_to_root(self):
        nav = NavigationState()
        nav.drill(_Node('A', ('A',), 1, 9), view_state='V0')
        nav.drill(_Node('B', ('A', 'B'), 2, 8), view_state='V1')
        nav.navigate(0, view_state='V2')
        self.assertEqual(nav.labels(), [])
        self.assertEqual(nav.current_scope(), ((), None))

    def test_navigate_pushes_history(self):
        nav = NavigationState()
        nav.drill(_Node('A', ('A',), 1, 9), view_state='V0')
        nav.navigate(0, view_state='V1')
        # going back from the root undoes the navigate, restoring scope A
        self.assertEqual(nav.back(), 'V1')
        self.assertEqual(nav.current_scope(), (('A',), (1, 9)))


class TestJump(unittest.TestCase):
    def test_jump_replaces_stack_with_chain(self):
        nav = NavigationState()
        nav.drill(_Node('A', ('A',), 1, 9), view_state='V0')
        chain = [{'label': 'Side', 'path': ('Side',), 'range': (40, 60)},
                 {'label': 'Inner', 'path': ('Side', 'Inner'), 'range': (45, 55)}]
        nav.jump(chain, view_state='V1')
        self.assertEqual(nav.labels(), ['Side', 'Inner'])
        self.assertEqual(nav.current_scope(), (('Side', 'Inner'), (45, 55)))

    def test_jump_pushes_history(self):
        nav = NavigationState()
        nav.drill(_Node('A', ('A',), 1, 9), view_state='V0')
        nav.jump([{'label': 'Side', 'path': ('Side',), 'range': (40, 60)}],
                 view_state='V1')
        self.assertEqual(nav.back(), 'V1')
        self.assertEqual(nav.current_scope(), (('A',), (1, 9)))

    def test_jump_to_empty_chain_is_root(self):
        nav = NavigationState()
        nav.drill(_Node('A', ('A',), 1, 9), view_state='V0')
        nav.jump([], view_state='V1')
        self.assertEqual(nav.current_scope(), ((), None))


class TestReset(unittest.TestCase):
    def test_reset_clears_stack_and_history(self):
        nav = NavigationState()
        nav.drill(_Node('A', ('A',), 1, 9), view_state='V0')
        nav.drill(_Node('B', ('A', 'B'), 2, 5), view_state='V1')
        nav.reset()
        self.assertEqual(nav.current_scope(), ((), None))
        self.assertEqual(nav.labels(), [])
        self.assertFalse(nav.can_back)


class TestHistoryBound(unittest.TestCase):
    def test_history_is_bounded(self):
        nav = NavigationState(history_limit=3)
        for i in range(10):
            nav.drill(_Node('N%d' % i, ('N%d' % i,), i, i), view_state='V%d' % i)
        # only the last 3 viewpoints survive; older ones drop silently
        depth = 0
        while nav.can_back:
            nav.back()
            depth += 1
        self.assertEqual(depth, 3)

    def test_default_limit_is_module_constant(self):
        nav = NavigationState()
        for i in range(NAV_HISTORY_LIMIT + 5):
            nav.drill(_Node('N%d' % i, ('N%d' % i,), i, i), view_state=i)
        depth = 0
        while nav.can_back:
            nav.back()
            depth += 1
        self.assertEqual(depth, NAV_HISTORY_LIMIT)


class TestHistoryIndependence(unittest.TestCase):
    def test_later_mutation_does_not_corrupt_snapshot(self):
        # pushed snapshots are independent copies
        nav = NavigationState()
        nav.drill(_Node('A', ('A',), 1, 9), view_state='V0')
        nav.drill(_Node('B', ('A', 'B'), 2, 5), view_state='V1')
        nav.back()                       # back to A
        nav.drill(_Node('C', ('A', 'C'), 6, 7), view_state='V2')
        nav.back()                       # back to A again
        self.assertEqual(nav.labels(), ['A'])
        nav.back()                       # back to root
        self.assertEqual(nav.labels(), [])


if __name__ == '__main__':
    unittest.main()
