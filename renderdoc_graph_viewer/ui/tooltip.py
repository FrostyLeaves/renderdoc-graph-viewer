# -*- coding: utf-8 -*-
"""Pure tooltip-text formatting for graph nodes and edges.

No Qt: takes plain (duck-typed graph_model) node/edge objects, so it is
unit-testable without PySide2."""

from ..graph_model import NODE_PASS, NODE_PORTAL, CAT_PORTAL, RES_BUFFER
from ..i18n import tr
from .style import (
    TOOLTIP_LIST_LIMIT as LIST_LIMIT,
    TOOLTIP_EDGE_EID_LIMIT as EDGE_EID_LIMIT,
)


def _is_pass_node(node):
    return getattr(node, 'node_type', None) in (NODE_PASS, NODE_PORTAL)


def format_edge_tooltip(edge):
    names = {}
    for eid, uname in edge.usages:
        names.setdefault(uname, []).append(eid)
    lines = ['[%s]' % edge.kind]
    for uname in sorted(names):
        eids = names[uname]
        shown = ', '.join(str(e) for e in eids[:EDGE_EID_LIMIT])
        if len(eids) > EDGE_EID_LIMIT:
            shown += ', ...(%d total)' % len(eids)
        lines.append('%s @ EID %s' % (uname, shown))
    return '\n'.join(lines)


def format_node_tooltip(node, episode_totals):
    """Multi-line tooltip for a pass / portal / resource node. episode_totals:
    {res_key: total write-version count} for the 'write version #n / N' line."""
    if _is_pass_node(node):
        lines = [node.name, 'kind: %s' % node.kind,
                 'EID %d - %d' % (node.first_eid, node.last_eid),
                 'actions: %d' % node.action_count]
        members = getattr(node, 'bundle_members', None)
        if members:
            lines.append(
                tr('Bundle of %d identical nodes (double-click a row '
                   'to jump to its event):') % len(members))
            for nm in members[:LIST_LIMIT]:
                lines.append(u'  · %s' % nm)
            if len(members) > LIST_LIMIT:
                lines.append(tr('  … +%d more')
                             % (len(members) - LIST_LIMIT))
        if node.marker_path:
            lines.append('marker: %s' % ' / '.join(node.marker_path))
        if getattr(node, 'kind', '') == CAT_PORTAL:
            if getattr(node, 'portal_focus_eid', None) is not None:
                where = (u' / '.join(node.portal_path)
                         if node.portal_path else tr('Whole frame'))
                lines.append(
                    tr('[external node - double-click to jump to "%s" '
                       'and locate it]') % where)
            else:
                lines.append(
                    tr('[external scope - double-click to jump '
                       'there]'))
        elif getattr(node, 'drillable', False):
            lines.append(tr('[double-click to enter]'))
        return '\n'.join(lines)
    info = node.info or {}
    lines = [node.name, 'kind: %s' % node.res_kind]
    members = getattr(node, 'bundle_members', None)
    if members:
        lines.append(
            tr('Bundle of %d identical resources:') % len(members))
        for nm in members[:LIST_LIMIT]:
            lines.append(u'  · %s' % nm)
        if len(members) > LIST_LIMIT:
            lines.append(tr('  … +%d more')
                         % (len(members) - LIST_LIMIT))
    gens = getattr(node, 'generations', 1)
    if gens > 1:
        lines.append(
            tr('%d write generations collapsed: every generation\'s '
               'read/write edges meet at this node; an early-gen '
               'reader linked to a late-gen writer is a temporal '
               'artifact') % gens)
    total = episode_totals.get(node.res_key, 1)
    if total > 1:
        lines.append(tr('write version: #%d / %d') %
                     (node.version, total))
    if info.get('dims'):
        lines.append(info['dims'])
    if info.get('format') and info['format'] != 'buffer':
        lines.append(info['format'])
    if getattr(node, 'scope_input', False):
        lines.append(
            tr('[scope input - written from outside the current '
               'scope]'))
    elif node.imported:
        lines.append(
            tr('[external - never written this frame; content '
               'from outside it]'))
    if getattr(node, 'internal', False):
        lines.append(tr('[internal - content never leaves this node]'))
    out_r = getattr(node, 'outside_readers', 0)
    out_w = getattr(node, 'outside_writers', 0)
    if out_r or out_w:
        lines.append(tr('outside scope: %d reads / %d writes') %
                     (out_r, out_w))
    lines.append('writers: %d   readers: %d' %
                 (len(node.writer_ids), len(node.reader_ids)))
    if node.res_kind != RES_BUFFER and not members:
        lines.append(
            tr('eye: click to cycle preview - collapsed -> raw '
               'range -> fitted range'))
    return '\n'.join(lines)
