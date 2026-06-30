# -*- coding: utf-8 -*-
"""Cleanup passes for RenderDoc usage events that are not resource refinements."""

from . import action_flags
from .apis._common import walk_actions


def _label_flag_masks(rd):
    """(marker_mask, exclude_mask) for API-agnostic debug-label detection.

    An action whose flags are purely marker-class executes nothing, so any usage
    on it is a phantom. exclude keeps structural/executing actions out.
    """
    exclude = (action_flags.structural(rd) |
               action_flags.flag(rd, 'MultiAction') |
               action_flags.executable(rd) |
               action_flags.flag(rd, 'Clear') |
               action_flags.transfer(rd) |
               action_flags.flag(rd, 'Present'))
    return action_flags.marker(rd), exclude


def collect_label_cleanup(controller, rd):
    """Return {'labels': [event ids]} for pure debug-label actions."""
    try:
        roots = controller.GetRootActions()
    except Exception:
        return {'labels': []}
    f_marker, f_not_label = _label_flag_masks(rd)
    label_eids = set()

    def on_action(action):
        if (action.flags & f_marker) and not (action.flags & f_not_label):
            label_eids.add(action.eventId)

    walk_actions(roots, on_action)
    return {'labels': sorted(label_eids)}


def strip_label_usages(usage_by_res, label_eids):
    """Drop usages on debug-label events. They execute nothing, so attachment
    usages RenderDoc tags on them are phantoms that would forge false writers."""
    if not label_eids:
        return
    for res_key, evs in usage_by_res.items():
        out = [(eid, uname) for (eid, uname) in evs if eid not in label_eids]
        if len(out) != len(evs):
            usage_by_res[res_key] = out


def apply_label_cleanup(usage_by_res, result):
    result = result or {}
    strip_label_usages(usage_by_res, frozenset(result.get('labels') or ()))
