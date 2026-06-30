# -*- coding: utf-8 -*-
"""i18n: English source strings pass through untouched; a per-language
table overrides them; missing entries fall back to the English source."""
import unittest

from renderdoc_graph_viewer import i18n


class TestI18n(unittest.TestCase):
    def setUp(self):
        self._saved = i18n._TRANSLATIONS
        i18n._lang = None

    def tearDown(self):
        i18n._TRANSLATIONS = self._saved
        i18n._lang = None

    def test_english_source_passthrough(self):
        # no table shipped (the production state) -> source string is returned
        i18n.set_language('en')
        self.assertEqual(i18n.tr('Refresh'), 'Refresh')

    def test_translation_table_used_when_present(self):
        i18n._TRANSLATIONS = {'zh': {'Refresh': u'刷新'}}
        i18n.set_language('zh')
        self.assertEqual(i18n.tr('Refresh'), u'刷新')
        # a key missing from the table still falls back to its source
        self.assertEqual(i18n.tr('Apply'), 'Apply')


if __name__ == '__main__':
    unittest.main()
