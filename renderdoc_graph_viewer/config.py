# -*- coding: utf-8 -*-
"""Persistent panel configuration: a flat bool dict JSON-persisted at
%APPDATA%/qrenderdoc/renderdoc_graph_viewer.json. Pure Python (no Qt, no
renderdoc) so it is unit-testable; any I/O or parse failure degrades to
DEFAULTS rather than taking down the UI.
"""

import json
import os

# Config keys. These strings are also the JSON field names, so their
# values must stay stable across releases (renaming a constant is fine,
# changing its string value orphans existing saved configs).
KEY_SHOW_EXTERNAL = 'show_external'
KEY_SHOW_INTERNAL = 'show_internal'
KEY_SHOW_ORPHANS = 'show_orphans'
KEY_SHOW_PORTALS = 'show_portals'
KEY_BUNDLING = 'bundling'
KEY_PARSE_SHADER = 'unused_scan'   # shader-source parsing / RW de-edge; value kept for back-compat
KEY_TEX_COLOR = 'tex_color'
KEY_TEX_DEPTH = 'tex_depth'
KEY_TEX_RW = 'tex_rw'
KEY_TEX_SWAP = 'tex_swap'
KEY_TEX_OTHER = 'tex_other'
KEY_BUF_RW = 'buf_rw'
KEY_BUF_INDIRECT = 'buf_indirect'
KEY_BUF_VERTEX_INDEX = 'buf_vertex_index'
KEY_BUF_CONSTANTS = 'buf_constants'
KEY_BUF_NOFLAGS = 'buf_noflags'

# Factory defaults: all candidate classes admitted, external inputs and
# orphans shown, internal working sets hidden.
DEFAULTS = {
    # display filters (batch-applied)
    KEY_SHOW_EXTERNAL: True,    # read-only-in-frame resources
    KEY_SHOW_INTERNAL: False,   # pure self-RW working sets
    KEY_SHOW_ORPHANS: True,     # passes with no RT dependencies
    KEY_SHOW_PORTALS: True,     # external-scope portal nodes
    # parse-level features (Apply-gated)
    KEY_BUNDLING: True,         # merge same-behaviour nodes
    # background features
    KEY_PARSE_SHADER: False,    # shader-source parsing: RW de-edge + unused dashing
    # resource candidates (re-extraction required).
    # Textures: which creationFlags classes may enter the graph.
    KEY_TEX_COLOR: True,
    KEY_TEX_DEPTH: True,
    KEY_TEX_RW: True,           # ShaderReadWrite (UAV/storage image)
    KEY_TEX_SWAP: True,         # swapchain backbuffers
    KEY_TEX_OTHER: True,        # everything else (sampled assets etc.)
    # Buffers: per-category admission (no master switch - clearing every
    # category excludes all buffers).
    KEY_BUF_RW: True,           # ReadWrite (SSBO/UAV)
    KEY_BUF_INDIRECT: True,
    KEY_BUF_VERTEX_INDEX: True,
    KEY_BUF_CONSTANTS: True,
    KEY_BUF_NOFLAGS: True,      # creationFlags == 0 (copy dest / readback)
}

CANDIDATE_KEYS = (
    KEY_TEX_COLOR, KEY_TEX_DEPTH, KEY_TEX_RW, KEY_TEX_SWAP, KEY_TEX_OTHER,
    KEY_BUF_RW, KEY_BUF_INDIRECT, KEY_BUF_VERTEX_INDEX,
    KEY_BUF_CONSTANTS, KEY_BUF_NOFLAGS,
)

DISPLAY_KEYS = (KEY_SHOW_EXTERNAL, KEY_SHOW_INTERNAL, KEY_SHOW_ORPHANS,
                KEY_SHOW_PORTALS)

# Apply-gated parse switches.
FEATURE_KEYS = (KEY_BUNDLING, KEY_PARSE_SHADER)


def candidates_of(cfg):
    """The extract-affecting subset; dirty-compare and extract_bundle key
    off this."""
    return dict((k, bool(cfg.get(k, DEFAULTS[k]))) for k in CANDIDATE_KEYS)


def display_of(cfg):
    """The display-filter subset; the rendered graph follows the APPLIED
    snapshot, not the live checkboxes."""
    return dict((k, bool(cfg.get(k, DEFAULTS[k]))) for k in DISPLAY_KEYS)


def features_of(cfg):
    """Apply-gated parse switches."""
    return dict((k, bool(cfg.get(k, DEFAULTS[k]))) for k in FEATURE_KEYS)


def dirty_flags(cfg, applied_candidates, applied_display, applied_features):
    """Return (candidate, display, feature) dirty flags.

    A missing applied snapshot is treated as clean."""
    cand = (applied_candidates is not None and
            candidates_of(cfg) != applied_candidates)
    disp = (applied_display is not None and
            display_of(cfg) != applied_display)
    feat = (applied_features is not None and
            features_of(cfg) != applied_features)
    return cand, disp, feat


def config_path():
    base = os.environ.get('APPDATA')
    if not base:
        base = os.path.join(os.path.expanduser('~'), '.config')
    return os.path.join(base, 'qrenderdoc', 'renderdoc_graph_viewer.json')


def load(path=None):
    """Read config; unknown keys dropped, values coerced to bool, any
    failure yields pure DEFAULTS."""
    cfg = dict(DEFAULTS)
    try:
        with open(path or config_path(), 'r') as f:
            data = json.load(f)
        for k in DEFAULTS:
            if k in data:
                cfg[k] = bool(data[k])
    except Exception:
        return dict(DEFAULTS)
    return cfg


def save(cfg, path=None):
    """Best-effort write; returns False rather than raising so a full disk
    can't break the panel."""
    p = path or config_path()
    try:
        d = os.path.dirname(p)
        if d and not os.path.isdir(d):
            os.makedirs(d)
        data = dict((k, bool(cfg.get(k, DEFAULTS[k]))) for k in DEFAULTS)
        with open(p, 'w') as f:
            json.dump(data, f, indent=2, sort_keys=True)
        return True
    except Exception:
        return False
