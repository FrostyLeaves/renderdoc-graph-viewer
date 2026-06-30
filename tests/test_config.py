# -*- coding: utf-8 -*-
"""Persistent config (config.py): DEFAULTS, the candidate / display / feature
subset extractors and their partition of DEFAULTS, dirty-flag comparison, and
load/save degradation (corrupt / partial / unknown-key -> DEFAULTS)."""
import json
import os
import shutil
import tempfile
import unittest

from renderdoc_graph_viewer import config


class TestDefaults(unittest.TestCase):
    def test_default_values(self):
        # factory defaults = the user's settled working set
        d = config.DEFAULTS
        # display filters: orphans shown; external and internal hidden
        self.assertFalse(d['show_external'])
        self.assertFalse(d['show_internal'])
        self.assertTrue(d['show_orphans'])
        self.assertTrue(d['show_portals'])
        # parse / features
        self.assertTrue(d['bundling'])
        self.assertFalse(d['parse_shader_access'])
        # candidates: every class admitted
        for key in config.CANDIDATE_KEYS:
            self.assertTrue(d[key], key)

    def test_candidates_of_extracts_only_candidate_keys(self):
        cfg = dict(config.DEFAULTS)
        cands = config.candidates_of(cfg)
        self.assertEqual(set(cands), set(config.CANDIDATE_KEYS))
        self.assertNotIn('show_external', cands)
        self.assertNotIn('bundling', cands)

    def test_display_of_extracts_only_display_keys(self):
        cfg = dict(config.DEFAULTS)
        cfg['show_internal'] = True
        disp = config.display_of(cfg)
        self.assertEqual(set(disp), set(config.DISPLAY_KEYS))
        self.assertTrue(disp['show_internal'])
        self.assertNotIn('tex_color', disp)

    def test_features_of_extracts_only_feature_keys(self):
        cfg = dict(config.DEFAULTS)
        feats = config.features_of(cfg)
        self.assertEqual(set(feats), set(config.FEATURE_KEYS))
        self.assertIn('bundling', feats)
        self.assertIn('parse_shader_access', feats)
        self.assertNotIn('show_external', feats)
        self.assertNotIn('tex_color', feats)

    def test_every_default_key_is_classified_exactly_once(self):
        # feature / display / candidate partition DEFAULTS with no overlap and
        # no leftovers, so a new key can't silently escape the dirty machine.
        feat = set(config.FEATURE_KEYS)
        disp = set(config.DISPLAY_KEYS)
        cand = set(config.CANDIDATE_KEYS)
        self.assertEqual(feat & disp, set())
        self.assertEqual(feat & cand, set())
        self.assertEqual(disp & cand, set())
        self.assertEqual(feat | disp | cand, set(config.DEFAULTS))


class TestDirtyFlags(unittest.TestCase):
    """Dirty-flag tests for the three switch classes."""

    def _applied(self, cfg):
        return (config.candidates_of(cfg), config.display_of(cfg),
                config.features_of(cfg))

    def test_clean_when_matching_applied(self):
        cfg = dict(config.DEFAULTS)
        ac, ad, af = self._applied(cfg)
        self.assertEqual(config.dirty_flags(cfg, ac, ad, af),
                         (False, False, False))

    def test_feature_change_marks_feature_only(self):
        cfg = dict(config.DEFAULTS)
        ac, ad, af = self._applied(cfg)
        cfg['bundling'] = not cfg['bundling']
        self.assertEqual(config.dirty_flags(cfg, ac, ad, af),
                         (False, False, True))

    def test_parse_shader_change_marks_feature_only(self):
        cfg = dict(config.DEFAULTS)
        ac, ad, af = self._applied(cfg)
        cfg['parse_shader_access'] = not cfg['parse_shader_access']
        self.assertEqual(config.dirty_flags(cfg, ac, ad, af),
                         (False, False, True))

    def test_display_change_marks_display_only(self):
        cfg = dict(config.DEFAULTS)
        ac, ad, af = self._applied(cfg)
        cfg['show_internal'] = not cfg['show_internal']
        self.assertEqual(config.dirty_flags(cfg, ac, ad, af),
                         (False, True, False))

    def test_candidate_change_marks_candidate_only(self):
        cfg = dict(config.DEFAULTS)
        ac, ad, af = self._applied(cfg)
        cfg['tex_color'] = not cfg['tex_color']
        self.assertEqual(config.dirty_flags(cfg, ac, ad, af),
                         (True, False, False))

    def test_none_candidate_snapshot_is_not_dirty(self):
        # candidate snapshot not yet landed
        cfg = dict(config.DEFAULTS)
        cfg['tex_color'] = not cfg['tex_color']
        _, ad, af = self._applied(cfg)
        self.assertEqual(config.dirty_flags(cfg, None, ad, af),
                         (False, False, False))


class TestLoadSave(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, 'cfg.json')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_load_missing_file_returns_defaults(self):
        cfg = config.load(self.path)
        self.assertEqual(cfg, dict(config.DEFAULTS))

    def test_roundtrip(self):
        cfg = dict(config.DEFAULTS)
        cfg['buf_noflags'] = True
        cfg['show_external'] = True
        self.assertTrue(config.save(cfg, self.path))
        back = config.load(self.path)
        self.assertEqual(back, cfg)

    def test_load_partial_file_merges_defaults(self):
        with open(self.path, 'w') as f:
            json.dump({'tex_other': True}, f)
        cfg = config.load(self.path)
        self.assertTrue(cfg['tex_other'])
        self.assertTrue(cfg['buf_rw'])          # untouched default
        self.assertFalse(cfg['show_internal'])  # untouched default

    def test_load_corrupt_file_returns_defaults(self):
        with open(self.path, 'w') as f:
            f.write('{not json!!')
        cfg = config.load(self.path)
        self.assertEqual(cfg, dict(config.DEFAULTS))

    def test_load_ignores_unknown_keys_and_coerces_bool(self):
        with open(self.path, 'w') as f:
            json.dump({'mystery': 1, 'show_orphans': 1, 'bundling': 0}, f)
        cfg = config.load(self.path)
        self.assertNotIn('mystery', cfg)
        self.assertIs(cfg['show_orphans'], True)
        self.assertIs(cfg['bundling'], False)

    def test_save_creates_parent_dirs(self):
        deep = os.path.join(self.tmp, 'a', 'b', 'cfg.json')
        self.assertTrue(config.save(dict(config.DEFAULTS), deep))
        self.assertTrue(os.path.isfile(deep))

    def test_save_failure_returns_false(self):
        # a directory path cannot be opened as a file for writing
        self.assertFalse(config.save(dict(config.DEFAULTS), self.tmp))


class TestConfigPath(unittest.TestCase):
    def test_uses_appdata_when_set(self):
        old = os.environ.get('APPDATA')
        os.environ['APPDATA'] = r'X:\fakeappdata'
        try:
            p = config.config_path()
        finally:
            if old is None:
                del os.environ['APPDATA']
            else:
                os.environ['APPDATA'] = old
        self.assertEqual(
            p, os.path.join(r'X:\fakeappdata', 'qrenderdoc',
                            'renderdoc_graph_viewer.json'))

    def test_falls_back_to_home_without_appdata(self):
        old = os.environ.pop('APPDATA', None)
        try:
            p = config.config_path()
        finally:
            if old is not None:
                os.environ['APPDATA'] = old
        self.assertIn('qrenderdoc', p)
        self.assertTrue(p.endswith('renderdoc_graph_viewer.json'))


if __name__ == '__main__':
    unittest.main()
