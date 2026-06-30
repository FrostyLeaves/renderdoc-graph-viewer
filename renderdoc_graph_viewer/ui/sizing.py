# -*- coding: utf-8 -*-
"""Pure node-sizing geometry: the (width, height) of a pass or resource node
from its model data. Qt-free — callers inject a text-width measure and an
is-expanded predicate — so the layout math is natively testable. The geometry
constants live here; graph_widget imports them back for painting."""

from ..graph_model import CAT_PORTAL, RES_BUFFER
from ..i18n import tr

PAD = 8.0
TITLE_H = 20.0
SUBLINE_H = 18.0        # EID / dims sub-row height under the title
BADGE_W = 26.0          # version-badge box reserved on a resource title row
BUNDLE_ROW_H = 15.0     # one row per bundled member name
BUNDLE_MAX_ROWS = 24    # beyond this an overflow row summarises the rest
PASS_BUNDLE_ROWS_Y = 42.0   # member rows start below the title + EID rows
THUMB_W = 192
THUMB_H = 108


def measure_member_rows(text_width, members, w0):
    """Widen w0 to fit up to BUNDLE_MAX_ROWS member names and report the row
    count (plus one overflow row when truncated). text_width(name) -> px. Shared
    by the pass / resource bundle sizing so paint and node_size stay in step."""
    shown = members[:BUNDLE_MAX_ROWS]
    w = w0
    for nm in shown:
        w = max(w, text_width(nm) + 4)
    rows = len(shown) + (1 if len(members) > BUNDLE_MAX_ROWS else 0)
    return max(w + 2 * PAD + 12, 170.0), rows


def node_size(node, is_pass, text_width, is_expanded):
    """Return (width, height) for a node. text_width(text, bold) -> px (titles
    paint bold, so measure them bold too or the node sizes a few percent short
    and elides forever); is_expanded(node) -> bool for a resource thumbnail."""
    if is_pass:
        if node.kind == CAT_PORTAL:
            sub = tr('External scope EID %d-%d') % (
                node.first_eid, node.last_eid)
        else:
            sub = 'EID %d-%d  (%d)' % (
                node.first_eid, node.last_eid, node.action_count)
        # the painted title is just node.name (no type glyphs), measured bold
        w = max(text_width(node.name, True), text_width(sub, False))
        members = getattr(node, 'bundle_members', None)
        if members:
            w, rows = measure_member_rows(
                lambda nm: text_width(nm, False), members, w)
            return (w, PASS_BUNDLE_ROWS_Y + rows * BUNDLE_ROW_H + 6.0)
        w = max(w + 2 * PAD + 6, 130.0)
        return (w, 46.0)
    members = getattr(node, 'bundle_members', None)
    if members:
        w, rows = measure_member_rows(
            lambda nm: text_width(nm, False), members,
            text_width(node.name, True))
        return (w, TITLE_H + rows * BUNDLE_ROW_H + 8.0)
    info = node.info or {}
    sub = '%s %s' % (info.get('dims', ''), info.get('format', ''))
    w = max(text_width(node.label(), False), text_width(sub, False))
    if getattr(node, 'version', 1) >= 2:
        w += BADGE_W  # episode badge shares the title row
    w = max(w + 2 * PAD + 12, 130.0)
    h = 42.0
    if node.res_kind != RES_BUFFER and is_expanded(node):
        w = max(w, THUMB_W + 2 * PAD)
        h += THUMB_H + 8
    return (w, h)
