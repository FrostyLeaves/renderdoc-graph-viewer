# -*- coding: utf-8 -*-
"""Pure edge-anchor fan-out: spread the attachment points of edges along each
node's side so they don't overlap. Qt-free so it can be tested natively; the
widget supplies each edge's endpoints and the y of its opposite end."""


def assign_anchor_fractions(edges):
    """edges: iterable of (key, src_id, dst_id, kind, sort_y_out, sort_y_in),
    where sort_y_out / sort_y_in are the y of the OTHER endpoint as seen from
    the source / destination side. Returns {key: (src_frac, dst_frac)}: along
    each node side the edges get evenly spaced fractions (i+1)/(n+1), ordered by
    the other end's y then (src, dst, kind) so ties stay deterministic."""
    out_by_node = {}
    in_by_node = {}
    for key, src_id, dst_id, kind, sort_y_out, sort_y_in in edges:
        tie = (src_id, dst_id, kind)
        out_by_node.setdefault(src_id, []).append((sort_y_out, tie, key))
        in_by_node.setdefault(dst_id, []).append((sort_y_in, tie, key))
    out_frac = {}
    in_frac = {}
    for by_node, dest in ((out_by_node, out_frac), (in_by_node, in_frac)):
        for entries in by_node.values():
            entries.sort(key=lambda t: (t[0], t[1]))
            n = len(entries)
            for i, (_y, _tie, key) in enumerate(entries):
                dest[key] = (i + 1.0) / (n + 1.0)
    return dict((key, (out_frac.get(key, 0.5), in_frac.get(key, 0.5)))
                for key in set(out_frac) | set(in_frac))
