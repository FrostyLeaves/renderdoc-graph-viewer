# -*- coding: utf-8 -*-
"""Pure status-bar line formatting (status.format_status / format_warning_tooltip)."""
import unittest

from renderdoc_graph_viewer.ui.status import (
    format_status, format_warning_tooltip, WARNING_TOOLTIP_LIMIT)


class TestFormatStatus(unittest.TestCase):
    def test_basic_line(self):
        s = format_status({'passes': 3, 'resources': 5, 'edges': 7,
                           'seconds': 1.234}, {}, [])
        self.assertEqual(s, '3 passes · 5 resources · 7 edges · 1.23s')

    def test_missing_stats_default_to_zero(self):
        self.assertEqual(format_status({}, {}, []),
                         '0 passes · 0 resources · 0 edges · 0.00s')

    def test_hidden_counts_appended_only_when_nonzero(self):
        s = format_status({'passes': 1, 'resources': 1, 'edges': 0,
                           'seconds': 0.0},
                          {'orphans': 2, 'external': 0, 'internal': 4}, [])
        self.assertIn('hidden:', s)
        self.assertIn('orphans 2', s)
        self.assertIn('internal 4', s)
        self.assertNotIn('external', s)   # zero count omitted

    def test_warnings_count_and_extra(self):
        s = format_status({'passes': 1, 'resources': 1, 'edges': 1,
                           'seconds': 0.0}, {}, ['w1', 'w2'], extra='busy')
        self.assertIn('2 warnings', s)
        self.assertTrue(s.endswith('· busy'))

    def test_no_hidden_no_warnings_no_extra(self):
        s = format_status({'passes': 1, 'resources': 0, 'edges': 0,
                           'seconds': 0.0}, {'orphans': 0}, [])
        self.assertNotIn('hidden', s)
        self.assertNotIn('warnings', s)


class TestFormatWarningTooltip(unittest.TestCase):
    def test_empty_or_none_is_empty_string(self):
        self.assertEqual(format_warning_tooltip(None), '')
        self.assertEqual(format_warning_tooltip([]), '')

    def test_lists_each_warning_on_its_own_line(self):
        self.assertEqual(format_warning_tooltip(['a', 'b', 'c']), 'a\nb\nc')

    def test_summarizes_overflow_past_the_limit(self):
        warns = ['w%d' % i for i in range(WARNING_TOOLTIP_LIMIT + 5)]
        tip = format_warning_tooltip(warns)
        lines = tip.split('\n')
        self.assertEqual(len(lines), WARNING_TOOLTIP_LIMIT + 1)  # +1 summary line
        self.assertEqual(lines[-1], '... (5 more)')
        self.assertEqual(lines[0], 'w0')

    def test_exactly_at_limit_has_no_summary(self):
        warns = ['w%d' % i for i in range(WARNING_TOOLTIP_LIMIT)]
        tip = format_warning_tooltip(warns)
        self.assertNotIn('more)', tip)
        self.assertEqual(len(tip.split('\n')), WARNING_TOOLTIP_LIMIT)


if __name__ == '__main__':
    unittest.main()
