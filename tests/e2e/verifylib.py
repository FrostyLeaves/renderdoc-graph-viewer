# -*- coding: utf-8 -*-
"""Shared qrenderdoc-side capture verification."""
import glob
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, '..', '..'))
for _p in (_REPO, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import compare


def newest_capture(scene_dir):
    caps = glob.glob(os.path.join(scene_dir, '*.rdc'))
    return max(caps, key=os.path.getmtime) if caps else None


def verify_capture(ctrl, rd, expected, log):
    """-> (ok, n_levels_checked) for all scope levels."""
    from renderdoc_graph_viewer import graph_model as gm

    bundle = gm.extract_bundle(ctrl, include_buffers=True, candidates=None,
                               parse_shaders=True)
    bundling = bool(expected.get('_bundling', False))
    state = {'ok': True, 'checked': 0}

    exp_levels = expected.get('refined', {})

    def walk(scope, rng):
        # key by marker INSTANCE (name#ordinal), so a marker that occurs more
        # than once is compared per-occurrence instead of all collapsing onto
        # one path-string key.
        instkey = compare.instance_key(bundle['leaves'], scope, rng)
        fg = gm.build_scoped(bundle, scope, rng, bundling=bundling)
        if instkey in exp_levels:
            nodes, edges = compare.canon(fg)
            exp = exp_levels[instkey]
            d = compare.diff(set(exp['nodes']), set(exp['edges']), nodes, edges)
            clean = compare.is_clean(d)
            state['checked'] += 1
            log('    %-9s level=%-14s %s' % ('refined', instkey or '(root)',
                                             'PASS' if clean else 'FAIL'))
            if not clean:
                state['ok'] = False
                log(compare.format_diff(d))
        # recurse into drillable children (one level deeper each)
        for p in fg.passes:
            if getattr(p, 'drillable', False):
                walk(tuple(p.marker_path), (p.first_eid, p.last_eid))

    walk((), None)

    ok = state['ok']
    checked = state['checked']

    # merged whole-frame view -- exercises the
    # build_passes(full-path) + non-versioned merged build_graph the focus path skips
    if 'merged' in expected:
        fg = gm.build_from_bundle(bundle, marker_depth=None, versioned=False)
        nodes, edges = compare.canon(fg)
        exp = expected['merged']
        d = compare.diff(set(exp['nodes']), set(exp['edges']), nodes, edges)
        clean = compare.is_clean(d)
        checked += 1
        log('    %-9s %s' % ('merged', 'PASS' if clean else 'FAIL'))
        if not clean:
            ok = False
            log(compare.format_diff(d))
    return ok, checked
