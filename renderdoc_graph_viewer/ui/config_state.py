# -*- coding: utf-8 -*-
"""Config state model: the single owner of the panel's editable settings (cfg)
and the applied snapshots the rendered graph / current extraction reflect, plus
the pure dirty-tracking and Apply-dispatch decision.

No Qt and no rendering here: the config band is a thin view that reads/writes
this model, and the controller drives the applied state. That keeps the one
piece of shared state (which display filters are in effect) in a single owner
instead of a widget others reach into - and makes the logic unit-testable
without PySide2.
"""

from .. import config


class ConfigState(object):
    def __init__(self, cfg=None, path=None):
        # path: where set() persists (None = the default user config path);
        # tests pass a temp path to avoid touching the real config.
        self._path = path
        self.cfg = cfg if cfg is not None else config.load(path)
        self.applied_candidates = None          # candidate set of the live bundle
        # Applied snapshots; cfg may hold pending (un-applied) edits.
        self.applied_display = config.display_of(self.cfg)
        self.applied_features = config.features_of(self.cfg)

    # ---- editable settings (the band writes one key per toggle) ----------
    def set(self, key, value):
        self.cfg[key] = bool(value)
        config.save(self.cfg, self._path)

    def candidate_config(self):
        """The extract-affecting subset, shaped for extract_bundle(candidates=)."""
        return config.candidates_of(self.cfg)

    def display_config(self):
        return config.display_of(self.cfg)

    def feature_config(self):
        return config.features_of(self.cfg)

    # ---- applied snapshots (committed on Apply / by the controller) ------
    def set_candidates_applied(self, cands):
        self.applied_candidates = dict(cands)

    def set_display_applied(self, display):
        self.applied_display = dict(display)

    def set_features_applied(self, features):
        self.applied_features = dict(features)

    # ---- dirty / apply decision (pure) -----------------------------------
    def dirty_state(self):
        return config.dirty_flags(self.cfg, self.applied_candidates,
                                  self.applied_display, self.applied_features)

    def shader_parse_dirty(self):
        if self.applied_features is None:
            return False
        key = config.KEY_PARSE_SHADER
        return (bool(self.cfg.get(key, config.DEFAULTS[key])) !=
                bool(self.applied_features.get(key, config.DEFAULTS[key])))

    def apply_button_state(self):
        """-> (enabled, reanalyze) for the Apply button."""
        cand, disp, feat = self.dirty_state()
        reanalyze = cand or self.shader_parse_dirty()
        return (cand or disp or feat), reanalyze

    def apply(self):
        """Decide what an Apply click does and commit the snapshots that take
        effect immediately (feature / display paths). The extract path commits
        nothing here - the controller commits it on a successful re-extract.

        -> (action, candidates, display, features) where action is one of
        'extract' | 'features' | 'display' | 'none'."""
        cand, disp, feat = self.dirty_state()
        shader = self.shader_parse_dirty()
        candidates = self.candidate_config()
        display = self.display_config()
        features = self.feature_config()
        if cand or shader:
            return ('extract', candidates, display, features)
        if feat:
            if disp:
                self.set_display_applied(display)
            self.set_features_applied(features)
            return ('features', candidates, display, features)
        if disp:
            self.set_display_applied(display)
            return ('display', candidates, display, features)
        return ('none', candidates, display, features)

    # ---- effective feature flags (the controller builds/extracts with) ---
    def bundling_enabled(self):
        feats = self.applied_features or self.feature_config()
        key = config.KEY_BUNDLING
        return bool(feats.get(key, config.DEFAULTS[key]))

    def shader_parsing_enabled(self):
        feats = self.applied_features or self.feature_config()
        key = config.KEY_PARSE_SHADER
        return bool(feats.get(key, config.DEFAULTS[key]))
