# -*- coding: utf-8 -*-
"""Pure widget logic: display filtering, selection closure, and the preview
tri-state cycle.

No Qt: operates on plain graph objects / dicts / edge pairs, so it is
unit-testable without PySide2."""

from .. import config as _config
from ..graph_model import CAT_PORTAL
from .style import (
    DIM_OPACITY, FILTER_DIM_OPACITY, MUTED_EDGE_OPACITY,
    EDGE_INDIRECT_OPACITY, Z_NODE_BASE, Z_NODE_HI,
)


def filter_visible(graph, display):
    """Apply a committed display-filter snapshot to graph's nodes. Returns
    (vis_passes, vis_resources, counts) where counts = {orphans, external,
    internal} hidden tallies."""
    counts = {'orphans': 0, 'external': 0, 'internal': 0}
    orphan_ids = getattr(graph, 'orphan_pass_ids', set())
    if display[_config.KEY_SHOW_ORPHANS]:
        vis_passes = list(graph.passes)
    else:
        vis_passes = [p for p in graph.passes if p.id not in orphan_ids]
    counts['orphans'] = len(graph.passes) - len(vis_passes)
    if not display[_config.KEY_SHOW_PORTALS]:
        vis_passes = [p for p in vis_passes if p.kind != CAT_PORTAL]

    show_external = display[_config.KEY_SHOW_EXTERNAL]
    show_internal = display[_config.KEY_SHOW_INTERNAL]
    vis_resources = []
    for rnode in graph.resources:
        scope_input = getattr(rnode, 'scope_input', False)
        if not show_external and rnode.imported and not scope_input:
            counts['external'] += 1
            continue
        if not show_internal and getattr(rnode, 'internal', False):
            # working set: content never leaves its single toucher
            counts['internal'] += 1
            continue
        vis_resources.append(rnode)
    return vis_passes, vis_resources, counts


def closure_of(start_id, edges):
    """Forward + backward reachable set from start_id over `edges` (an iterable
    of (src_id, dst_id) pairs)."""
    fwd = {}
    rev = {}
    for src, dst in edges:
        fwd.setdefault(src, []).append(dst)
        rev.setdefault(dst, []).append(src)
    keep = set([start_id])
    for adj in (fwd, rev):
        stack = [start_id]
        while stack:
            nid = stack.pop()
            for m in adj.get(nid, ()):
                if m not in keep:
                    keep.add(m)
                    stack.append(m)
    return keep


def visual_state(nodes, edges, selected_id, filter_text):
    """Selection/filter styling decision, Qt-free.

    nodes: iterable of (node_id, is_pass, label, res_key) (res_key None for a
           pass); label is matched case-insensitively against filter_text.
    edges: iterable of (edge_key, src_id, dst_id).
    Returns (node_state, edge_state):
      node_state[node_id] = {'opacity', 'z', 'selected'}
      edge_state[edge_key] = {'emphasis' in normal|direct|indirect|muted,
                              'opacity'}
    An edge whose endpoint is not among `nodes` is omitted (as the widget skips
    an edge with a missing node item)."""
    edges = list(edges)
    keep = None
    if selected_id:
        keep = closure_of(selected_id, ((s, d) for _k, s, d in edges))
    nodes = list(nodes)
    sel_res_key = None
    for nid, is_pass, _label, res_key in nodes:
        if nid == selected_id and not is_pass:
            sel_res_key = res_key
    ftext = (filter_text or '').strip().lower()
    node_state = {}
    node_op = {}
    for nid, is_pass, label, res_key in nodes:
        op = 1.0
        if ftext and ftext not in (label or '').lower():
            op = min(op, FILTER_DIM_OPACITY)
        if keep is not None and nid not in keep:
            op = min(op, DIM_OPACITY)
        node_op[nid] = op
        in_keep = keep is not None and nid in keep
        is_sel = (nid == selected_id or
                  (sel_res_key is not None and not is_pass and
                   res_key == sel_res_key))
        node_state[nid] = {'opacity': op,
                           'z': Z_NODE_HI if in_keep else Z_NODE_BASE,
                           'selected': is_sel}
    edge_state = {}
    for ekey, src, dst in edges:
        if src not in node_op or dst not in node_op:
            continue
        edge_op = min(node_op[src], node_op[dst])
        if keep is None:
            edge_state[ekey] = {'emphasis': 'normal', 'opacity': edge_op}
        elif src == selected_id or dst == selected_id:
            edge_state[ekey] = {'emphasis': 'direct', 'opacity': edge_op}
        elif src in keep and dst in keep:
            edge_state[ekey] = {'emphasis': 'indirect',
                                'opacity': min(edge_op, EDGE_INDIRECT_OPACITY)}
        else:
            edge_state[ekey] = {'emphasis': 'muted',
                                'opacity': min(edge_op, MUTED_EDGE_OPACITY)}
    return node_state, edge_state


def next_match(matches, text, prev_text, prev_idx):
    """Cycle the label-search selection. matches: (x, y, node_id) tuples already
    filtered to those whose label contains `text`. Returns (idx, node_id) for
    the next match ordered left-to-right then top-to-bottom, restarting at 0 when
    `text` differs from `prev_text`; (-1, None) when there are no matches."""
    if not matches:
        return -1, None
    ordered = sorted(matches)
    idx = -1 if text != prev_text else prev_idx
    idx = (idx + 1) % len(ordered)
    return idx, ordered[idx][2]


def cycle_expanded(expanded, key):
    """Advance the preview tri-state for `key`, mutating `expanded` in place:
    absent -> raw (False), raw -> fitted (True), fitted -> collapsed (removed).
    Returns True when entering an expanded state, so the caller drops the cached
    pixmap and the next grab uses the right range."""
    if key not in expanded:
        expanded[key] = False             # collapsed -> raw
        return True
    if not expanded[key]:
        expanded[key] = True              # raw -> fitted
        return True
    del expanded[key]                     # fitted -> collapsed
    return False
