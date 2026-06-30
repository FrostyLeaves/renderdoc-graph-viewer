# -*- coding: utf-8 -*-
"""Pure breadcrumb formatting tests."""
import unittest

from renderdoc_graph_viewer.ui import breadcrumb


class TestFormatBreadcrumb(unittest.TestCase):
    def test_single_label_is_bold_and_not_a_link(self):
        html = breadcrumb.format_breadcrumb(['Whole frame'])
        self.assertEqual(html, '<b>Whole frame</b>')

    def test_index_hrefs_and_last_is_bold(self):
        html = breadcrumb.format_breadcrumb(
            ['Whole frame', 'Shadows', 'Cascade 0'])
        self.assertIn('<a href="0"', html)
        self.assertIn('<a href="1"', html)
        self.assertNotIn('<a href="2"', html)   # current scope is bold, no link
        self.assertTrue(html.endswith('<b>Cascade 0</b>'))
        self.assertEqual(html.count(breadcrumb.SEP), 2)

    def test_escapes_html_in_marker_names(self):
        # marker-name HTML escaping
        html = breadcrumb.format_breadcrumb(['Root', 'A & B <draw>'])
        self.assertIn('A &amp; B &lt;draw&gt;', html)
        self.assertNotIn('<draw>', html)

    def test_link_color_is_applied_to_non_current_segments(self):
        html = breadcrumb.format_breadcrumb(['Root', 'Child'])
        self.assertIn('color:%s;' % breadcrumb.LINK_COLOR, html)

    def test_empty_chain_is_empty_string(self):
        self.assertEqual(breadcrumb.format_breadcrumb([]), '')


if __name__ == '__main__':
    unittest.main()
