# -*- coding: utf-8 -*-
"""Config state model: editable cfg + applied snapshots + dirty/apply decision.
Pure, no PySide2 - this is the logic the config band used to own inline."""
import os
import shutil
import tempfile
import unittest

from renderdoc_graph_viewer import config
from renderdoc_graph_viewer.ui.config_state import ConfigState


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='rdgv_cfgstate_')
        self.path = os.path.join(self.tmp, 'cfg.json')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _state(self):
        # start from a clean, fully-applied snapshot of DEFAULTS
        return ConfigState(cfg=dict(config.DEFAULTS), path=self.path)


class TestDirty(_Base):
    def test_clean_when_cfg_equals_applied(self):
        s = self._state()
        # applied_candidates starts None -> treated as clean
        s.set_candidates_applied(s.candidate_config())
        self.assertEqual(s.dirty_state(), (False, False, False))
        self.assertEqual(s.apply_button_state(), (False, False))

    def test_display_toggle_is_display_dirty(self):
        s = self._state()
        s.set(config.KEY_SHOW_EXTERNAL, not config.DEFAULTS[config.KEY_SHOW_EXTERNAL])
        cand, disp, feat = s.dirty_state()
        self.assertTrue(disp)
        self.assertFalse(cand)
        enabled, reanalyze = s.apply_button_state()
        self.assertTrue(enabled)
        self.assertFalse(reanalyze)   # display change does not re-extract

    def test_candidate_toggle_triggers_reanalyze(self):
        s = self._state()
        s.set_candidates_applied(s.candidate_config())
        s.set(config.KEY_TEX_COLOR, not config.DEFAULTS[config.KEY_TEX_COLOR])
        cand, _disp, _feat = s.dirty_state()
        self.assertTrue(cand)
        self.assertEqual(s.apply_button_state(), (True, True))   # re-analyze

    def test_shader_parse_toggle_is_reanalyze(self):
        s = self._state()
        s.set(config.KEY_PARSE_SHADER, not config.DEFAULTS[config.KEY_PARSE_SHADER])
        self.assertTrue(s.shader_parse_dirty())
        self.assertEqual(s.apply_button_state(), (True, True))

    def test_set_persists(self):
        s = self._state()
        s.set(config.KEY_SHOW_ORPHANS, False)
        reloaded = config.load(self.path)
        self.assertFalse(reloaded[config.KEY_SHOW_ORPHANS])


class TestApplyDispatch(_Base):
    def test_extract_path(self):
        s = self._state()
        s.set_candidates_applied(s.candidate_config())
        s.set(config.KEY_TEX_DEPTH, not config.DEFAULTS[config.KEY_TEX_DEPTH])
        action, cands, _d, _f = s.apply()
        self.assertEqual(action, 'extract')
        # extract path leaves applied candidates unchanged
        self.assertNotEqual(cands, s.applied_candidates)

    def test_features_path_commits_applied(self):
        s = self._state()
        s.set(config.KEY_BUNDLING, not config.DEFAULTS[config.KEY_BUNDLING])
        action, _c, _d, features = s.apply()
        self.assertEqual(action, 'features')
        self.assertEqual(s.applied_features, features)   # committed immediately
        # now clean
        self.assertEqual(s.dirty_state()[2], False)

    def test_display_path_commits_applied(self):
        s = self._state()
        s.set(config.KEY_SHOW_PORTALS, not config.DEFAULTS[config.KEY_SHOW_PORTALS])
        action, _c, display, _f = s.apply()
        self.assertEqual(action, 'display')
        self.assertEqual(s.applied_display, display)
        self.assertEqual(s.dirty_state()[1], False)

    def test_none_when_clean(self):
        s = self._state()
        s.set_candidates_applied(s.candidate_config())
        self.assertEqual(s.apply()[0], 'none')


class TestEffectiveFlags(_Base):
    def test_bundling_follows_applied_features(self):
        s = self._state()
        # applied feature snapshot controls the effective value
        s.set(config.KEY_BUNDLING, False)
        self.assertTrue(s.bundling_enabled())   # applied snapshot unchanged
        s.set_features_applied(s.feature_config())
        self.assertFalse(s.bundling_enabled())  # now committed

    def test_shader_parsing_flag(self):
        s = self._state()
        self.assertEqual(s.shader_parsing_enabled(),
                         bool(config.DEFAULTS[config.KEY_PARSE_SHADER]))


if __name__ == '__main__':
    unittest.main()
