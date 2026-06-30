# -*- coding: utf-8 -*-
"""graph_widget geometry tests."""
import unittest

try:
    from renderdoc_graph_viewer.ui import graph_widget as gw
    HAS_QT = True
except Exception:
    HAS_QT = False


@unittest.skipUnless(HAS_QT, 'PySide2 not available')
class TestBadgeEyeLayout(unittest.TestCase):
    """Write-version badge and preview-eye layout."""

    def test_badge_clears_eye_when_eye_present(self):
        for w in (130.0, 180.0, 260.0, 400.0):
            badge = gw._badge_rect_at(w, True)
            eye = gw._eye_rect_at(w)
            self.assertFalse(
                badge.intersects(eye),
                'badge %r overlaps eye %r at w=%.0f' % (badge, eye, w))

    def test_badge_uses_right_edge_without_eye(self):
        w = 200.0
        badge = gw._badge_rect_at(w, False)
        self.assertAlmostEqual(badge.right(), w - gw.PAD)


@unittest.skipUnless(HAS_QT, 'PySide2 not available')
class TestVersionBadge(unittest.TestCase):
    """The #n write-version badge shows for EVERY version of a multi-write
    resource (including #1), and nothing at all for a single-write
    resource."""

    class _Res(object):
        def __init__(self, version, version_count):
            self.version = version
            self.version_count = version_count

    def test_single_write_resource_has_no_badge(self):
        self.assertEqual(gw._version_badge(self._Res(1, 1)), '')

    def test_first_of_many_shows_hash_one(self):
        self.assertEqual(gw._version_badge(self._Res(1, 3)), '#1')

    def test_later_version_shows_its_number(self):
        self.assertEqual(gw._version_badge(self._Res(2, 3)), '#2')


@unittest.skipUnless(HAS_QT, 'PySide2 not available')
class TestPassTitle(unittest.TestCase):
    """Pass-side nodes show only their name - no type glyph: no gear on
    compute, no arrow on drillable scopes, no portal mark. compute / scope /
    portal are told apart by colour + shape."""

    class _Pass(object):
        def __init__(self, name, kind, drillable=False):
            self.name = name
            self.kind = kind
            self.drillable = drillable

    def test_pass_title_is_plain_name_for_every_kind(self):
        # pass title is plain text for every kind
        self.assertEqual(gw._pass_title(self._Pass('Lighting', 'compute')),
                         'Lighting')
        self.assertEqual(
            gw._pass_title(self._Pass('Shadows', 'scope', drillable=True)),
            'Shadows')
        self.assertEqual(gw._pass_title(self._Pass('Cull', 'portal')), 'Cull')


@unittest.skipUnless(HAS_QT, 'PySide2 not available')
class TestConfigApplyGating(unittest.TestCase):
    """Every config switch — including the parse-level Bundling and Parse-shader
    features — batches behind Apply: toggling only lights up Apply, the work
    fires on Apply, and the precedence is candidate > feature > display."""

    @classmethod
    def setUpClass(cls):
        cls.app = (gw.QtWidgets.QApplication.instance() or
                   gw.QtWidgets.QApplication([]))

    def setUp(self):
        from unittest import mock
        # never touch the user's real config file from a test
        p_load = mock.patch.object(gw._config, 'load',
                                   return_value=dict(gw._config.DEFAULTS))
        p_save = mock.patch.object(gw._config, 'save', return_value=True)
        p_load.start(); p_save.start()
        self.addCleanup(p_load.stop); self.addCleanup(p_save.stop)

    class _Callbacks(dict):
        """Memoised Mock per key so the panel and the test see the same one."""
        def __missing__(self, key):
            from unittest import mock
            m = mock.Mock(); self[key] = m; return m

    def _panel(self):
        cbs = self._Callbacks()
        panel = gw.GraphPanel(cbs)
        self.addCleanup(panel.deleteLater)
        return panel, cbs

    def test_apply_disabled_initially(self):
        panel, _ = self._panel()
        self.assertFalse(panel.apply_btn.isEnabled())

    def test_bundling_toggle_is_apply_gated(self):
        panel, cbs = self._panel()
        panel.bundle_cb.setChecked(not panel.bundle_cb.isChecked())
        cbs['features_apply'].assert_not_called()        # not instant anymore
        self.assertTrue(panel.apply_btn.isEnabled())
        panel._on_apply()
        cbs['features_apply'].assert_called_once()        # fires on Apply
        cbs['extract_apply'].assert_not_called()
        cbs['display_changed'].assert_not_called()
        self.assertFalse(panel.apply_btn.isEnabled())     # committed → clean

    def test_parse_shader_toggle_reanalyzes(self):
        panel, cbs = self._panel()
        panel.parse_shader_cb.setChecked(not panel.parse_shader_cb.isChecked())
        cbs['extract_apply'].assert_not_called()
        cbs['features_apply'].assert_not_called()
        self.assertTrue(panel.apply_btn.isEnabled())
        self.assertEqual(panel.apply_btn.text(), gw.tr('Apply & re-analyze'))
        panel._on_apply()
        cbs['extract_apply'].assert_called_once()
        cbs['features_apply'].assert_not_called()
        self.assertTrue(panel.apply_btn.isEnabled())     # waits for success
        cands, display, features = cbs['extract_apply'].call_args[0]
        panel.set_candidates_applied(cands)
        panel.set_display_applied(display)
        panel.set_features_applied(features)
        self.assertFalse(panel.apply_btn.isEnabled())

    def test_bundling_uses_applied_snapshot(self):
        panel, _cbs = self._panel()
        old = panel.bundling_enabled()
        panel.bundle_cb.setChecked(not panel.bundle_cb.isChecked())
        self.assertEqual(panel.bundling_enabled(), old)
        panel.set_features_applied(panel.feature_config())
        self.assertEqual(panel.bundling_enabled(), not old)

    def test_display_filter_routes_to_display_not_features(self):
        # display filter apply path
        panel, cbs = self._panel()
        box = panel._cfg_boxes[gw._config.KEY_SHOW_INTERNAL]
        box.setChecked(not box.isChecked())
        self.assertTrue(panel.apply_btn.isEnabled())
        panel._on_apply()
        cbs['display_changed'].assert_called_once()
        cbs['features_apply'].assert_not_called()
        cbs['extract_apply'].assert_not_called()


if __name__ == '__main__':
    unittest.main()
