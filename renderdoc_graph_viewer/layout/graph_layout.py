# -*- coding: utf-8 -*-
"""Graph layout adapter — delegates to the single GKNV (Graphviz dot-style)
engine in layout_gknv (network-simplex ranking, weighted-median ordering,
auxiliary-graph simplex coordinates). Edges draw as direct splines.

A deterministic backfill places any node the engine could not position. This is
defensive only: GKNV already covers isolated nodes, disconnected components,
cycles (back edges dropped with a warning) and empty input.
"""

COL_GAP = 120.0
Y_GAP = 28.0

_FALLBACK_W = 120.0
_FALLBACK_H = 50.0


def compute_layout_gknv(nodes, edges, sizes, rank_edges=None):
    """dot-style GKNV engine (network-simplex ranking, median ordering,
    auxiliary-graph simplex coordinates) in layout_gknv. Ranking obeys
    rank_edges when given."""
    from . import layout_gknv
    ids = [n.id for n in nodes]
    order_hint = {}
    for n in nodes:
        o = getattr(n, 'order', None)
        if o is not None:
            order_hint[n.id] = o
    draw_pairs = [(e.src_id, e.dst_id) for e in edges]
    rp = None
    if rank_edges is not None:
        rp = [(e.src_id, e.dst_id) for e in rank_edges]
    return layout_gknv.compute(ids, sizes, draw_pairs, rank_pairs=rp,
                               col_gap=COL_GAP, node_gap=Y_GAP,
                               order_hint=order_hint)


def _backfill_missing(nodes, sizes, positions):
    """Deterministically place any ids GKNV did not position, stacked in a new
    column to the right. GKNV covers every connected/isolated node, so this
    only fires if compute() raised — it guarantees the caller never sees a node
    without coordinates."""
    missing = [n.id for n in nodes if n.id not in positions]
    if not missing:
        return positions
    positions = dict(positions)
    xs = [xy[0] for xy in positions.values()]
    base_x = (max(xs) if xs else 0.0) + COL_GAP
    y = 0.0
    for nid in missing:
        _w, h = sizes.get(nid, (_FALLBACK_W, _FALLBACK_H))
        positions[nid] = (base_x, y)
        y += h + Y_GAP
    return positions


def _gknv_or_backfill(nodes, edges, sizes, rank_edges):
    try:
        positions, warns = compute_layout_gknv(
            nodes, edges, sizes, rank_edges=rank_edges)
        warns = list(warns)
    except Exception as exc:
        positions = {}
        warns = ['layout engine failed (%s); using fallback placement' % exc]
    return _backfill_missing(nodes, sizes, positions), warns


def compute_layout(nodes, edges, sizes, rank_edges=None):
    """(positions, warnings) via the GKNV engine."""
    return _gknv_or_backfill(nodes, edges, sizes, rank_edges)
