# -*- coding: utf-8 -*-
"""Layered graph layout (Sugiyama-style), pure Python, deterministic.

Pipeline: longest-path ranking (Kahn; cycle fallback: execution-order +
warning) -> dummy nodes for multi-rank forward edges -> median-sweep +
transpose ordering -> per-rank X columns, neighbor-median Y alignment.

compute_layout_full() returns routes (waypoints through dummy nodes) and
stubs (long high-fanout edges the caller renders as reference chips).
Backward and same-rank edges are always drawn directly.
"""

import bisect

X_GAP = 90.0
Y_GAP = 28.0
COL_GAP = 120.0
DUMMY_W = 8.0
DUMMY_H = 8.0
_ALIGN_PASSES = 3
_TRANSPOSE_ROUNDS = 4
_DUMMY_SPAN_CAP = 400  # cap dummy chain length against rank blowups


def _adjacency(ids, idset, edge_list):
    out_adj = dict((nid, []) for nid in ids)
    in_adj = dict((nid, []) for nid in ids)
    seen_pairs = set()
    for e in edge_list:
        pair = (e.src_id, e.dst_id)
        if e.src_id not in idset or e.dst_id not in idset or pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        out_adj[e.src_id].append(e.dst_id)
        in_adj[e.dst_id].append(e.src_id)
    return out_adj, in_adj


def _compute_ranks(nodes, ids, idset, rank_edge_list):
    warnings = []
    rank_out, rank_in = _adjacency(ids, idset, rank_edge_list)
    rank = dict((nid, 0) for nid in ids)
    indeg = dict((nid, len(rank_in[nid])) for nid in ids)
    queue = [nid for nid in ids if indeg[nid] == 0]
    qi = 0
    processed = 0
    while qi < len(queue):
        nid = queue[qi]
        qi += 1
        processed += 1
        for m in rank_out[nid]:
            if rank[m] < rank[nid] + 1:
                rank[m] = rank[nid] + 1
            indeg[m] -= 1
            if indeg[m] == 0:
                queue.append(m)
    if processed < len(ids):
        warnings.append('dependency cycle detected - using execution-order layout')
        # densify order values: portals carry order >= PORTAL_ORDER_BASE
        # (graph_model); raw 2*order would explode dummy chains downstream
        orders = dict((n.id, getattr(n, 'order', None)) for n in nodes)
        distinct = sorted(set(o for o in orders.values() if o is not None))
        dense = dict((o, i) for i, o in enumerate(distinct))
        for n in nodes:
            o = orders[n.id]
            if o is not None:
                rank[n.id] = 2 * dense[o]
        for n in nodes:
            if orders[n.id] is None:
                wr = [rank.get(w, 0) for w in getattr(n, 'writer_ids', [])]
                rank[n.id] = (max(wr) + 1) if wr else 0
    else:
        # ALAP: push each node as late as its consumers allow so inputs hug
        # the passes that consume them. queue is topological, so walking it
        # backwards finalises successors first.
        for nid in reversed(queue):
            if rank_out[nid]:
                best = min(rank[m] for m in rank_out[nid]) - 1
                if best > rank[nid]:
                    rank[nid] = best
    return rank, warnings


def _median(vals):
    if not vals:
        return None
    vals = sorted(vals)
    m = len(vals)
    if m % 2 == 1:
        return float(vals[m // 2])
    return (vals[m // 2 - 1] + vals[m // 2]) / 2.0


DEFAULT_STUB_SIZE = (120.0, 18.0)


def compute_layout(nodes, edges, sizes, rank_edges=None):
    """(positions, warnings). Routing is computed internally but discarded;
    positions already benefit from it."""
    positions, warnings, _routes, _stubs, _sp = compute_layout_full(
        nodes, edges, sizes, rank_edges=rank_edges, stub_span=None)
    return positions, warnings


def compute_layout_full(nodes, edges, sizes, rank_edges=None, stub_span=4,
                        stub_sizes=None, stub_min_fanout=4):
    """Returns (positions, warnings, routes, stubs, stub_pos).

    An edge becomes a stub (chip, not a long line) only when its span
    exceeds stub_span AND its source has >= stub_min_fanout drawn edges, so
    genuine hub spider-webs collapse but low-fanout long deps stay routed.
    With stub_sizes ({edge key: (w, h)}) each stub also becomes a layout
    node in the column before its consumer (position in stub_pos); without
    it the chips are hidden and stub_pos is empty."""
    warnings = []
    ids = [n.id for n in nodes]
    if not ids:
        return {}, warnings, {}, [], {}
    idset = set(ids)

    rank, rank_warns = _compute_ranks(
        nodes, ids, idset, edges if rank_edges is None else rank_edges)
    warnings.extend(rank_warns)

    out_degree = {}
    for e in edges:
        if e.src_id in idset and e.dst_id in idset:
            out_degree[e.src_id] = out_degree.get(e.src_id, 0) + 1

    # ------------------------------------------------ edge classification
    seen = set()
    routed = {}      # key -> [dummy ids] in rank order
    stubs = []       # keys rendered as reference chips by the caller
    dummy_rank = {}
    dummies = []
    stub_node = {}   # key -> stub layout-node id (only when stub_sizes given)
    stub_id_key = {}
    forward_links = []  # (a, b) ordering links, rank[a] < rank[b]

    for e in edges:
        if e.src_id not in idset or e.dst_id not in idset:
            continue
        key = (e.src_id, e.dst_id, getattr(e, 'kind', ''))
        if key in seen:
            continue
        seen.add(key)
        span = rank[e.dst_id] - rank[e.src_id]
        if (stub_span is not None and abs(span) > stub_span and
                out_degree.get(e.src_id, 0) >= stub_min_fanout):
            stubs.append(key)
            if stub_sizes is not None:
                sid = '!s!%s!%s!%s' % key
                srank = max(0, rank[e.dst_id] - 1)
                stub_node[key] = sid
                stub_id_key[sid] = key
                dummy_rank[sid] = srank
                forward_links.append((sid, e.dst_id))
            continue
        if span == 1:
            forward_links.append((e.src_id, e.dst_id))
            continue
        if span <= 0:
            continue  # flat/backward: drawn directly, not ordered
        if span > _DUMMY_SPAN_CAP:
            forward_links.append((e.src_id, e.dst_id))  # too long to route
            continue
        chain = []
        for r in range(rank[e.src_id] + 1, rank[e.dst_id]):
            did = '~%s~%s~%s~%d' % (key[0], key[1], key[2], r)
            chain.append(did)
            dummy_rank[did] = r
            dummies.append(did)
        routed[key] = chain
        prev = e.src_id
        for did in chain:
            forward_links.append((prev, did))
            prev = did
        forward_links.append((prev, e.dst_id))

    all_rank = dict(rank)
    all_rank.update(dummy_rank)

    nbr_out = {}
    nbr_in = {}
    for a, b in forward_links:
        nbr_out.setdefault(a, []).append(b)
        nbr_in.setdefault(b, []).append(a)

    # ------------------------------------------------------------- layers
    by_rank = {}
    for nid in ids:
        by_rank.setdefault(rank[nid], []).append(nid)
    for did in dummies:
        by_rank.setdefault(dummy_rank[did], []).append(did)
    for sid in stub_id_key:
        by_rank.setdefault(dummy_rank[sid], []).append(sid)

    order_hint = {}
    for n in nodes:
        order_hint[n.id] = getattr(n, 'order', None)

    ranks_sorted = sorted(by_rank.keys())
    pos_in = {}
    for r in ranks_sorted:
        layer = by_rank[r]
        layer.sort(key=lambda nid: (0, order_hint.get(nid), nid)
                   if order_hint.get(nid) is not None else (1, 0, nid))
        for i, nid in enumerate(layer):
            pos_in[nid] = i

    # ------------------------------------------- ordering: median sweeps
    def sweep(forward):
        seq = ranks_sorted if forward else list(reversed(ranks_sorted))
        for r in seq:
            layer = by_rank[r]

            def keyf(nid):
                nbrs = nbr_in.get(nid, ()) if forward else nbr_out.get(nid, ())
                med = _median([pos_in[m] for m in nbrs])
                if med is None:
                    med = float(pos_in[nid])
                return (med, pos_in[nid], nid)

            layer.sort(key=keyf)
            for i, nid in enumerate(layer):
                pos_in[nid] = i

    def pair_cross(a, b):
        # crossings when a sits directly above b
        c = 0
        for adj in (nbr_in, nbr_out):
            pa = sorted(pos_in[m] for m in adj.get(a, ()))
            pb = sorted(pos_in[m] for m in adj.get(b, ()))
            for x in pa:
                c += bisect.bisect_left(pb, x)
        return c

    def transpose():
        for _round in range(_TRANSPOSE_ROUNDS):
            improved = False
            for r in ranks_sorted:
                layer = by_rank[r]
                for i in range(len(layer) - 1):
                    a, b = layer[i], layer[i + 1]
                    if pair_cross(a, b) > pair_cross(b, a):
                        layer[i], layer[i + 1] = b, a
                        pos_in[a], pos_in[b] = pos_in[b], pos_in[a]
                        improved = True
            if not improved:
                break

    sweep(True)
    sweep(False)
    transpose()
    sweep(True)
    transpose()

    # -------------------------------------------------------- X: columns
    def size_of(nid):
        if nid in stub_id_key:
            return stub_sizes.get(stub_id_key[nid], DEFAULT_STUB_SIZE)
        if nid in dummy_rank:
            return (DUMMY_W, DUMMY_H)
        return sizes[nid]

    col_x = {}
    col_w = {}
    x = 0.0
    for r in ranks_sorted:
        w = max(size_of(nid)[0] for nid in by_rank[r])
        col_x[r] = x
        col_w[r] = w
        x += w + X_GAP

    # ------------------------------- Y: stack + neighbor-median alignment
    center_y = {}
    for r in ranks_sorted:
        layer = by_rank[r]
        total_h = (sum(size_of(nid)[1] for nid in layer) +
                   Y_GAP * (len(layer) - 1))
        y = -total_h / 2.0
        for nid in layer:
            h = size_of(nid)[1]
            center_y[nid] = y + h / 2.0
            y += h + Y_GAP

    def align_pass(forward):
        seq = ranks_sorted if forward else list(reversed(ranks_sorted))
        for r in seq:
            layer = by_rank[r]
            meds = []
            for nid in layer:
                nbrs = list(nbr_in.get(nid, ())) + list(nbr_out.get(nid, ()))
                meds.append(_median([center_y[m] for m in nbrs]))
            # neighbourless nodes gravitate to the column body instead of
            # being stranded at the initial stack position
            anchored = [m for m in meds if m is not None]
            column_pull = _median(anchored)
            desired = []
            for nid, med in zip(layer, meds):
                if med is None:
                    med = (column_pull if column_pull is not None
                           else center_y[nid])
                desired.append(med)
            # forward: enforce minimum separation going down
            placed = []
            prev_bottom = None
            for nid, want in zip(layer, desired):
                h = size_of(nid)[1]
                top = want - h / 2.0
                if prev_bottom is not None and top < prev_bottom + Y_GAP:
                    top = prev_bottom + Y_GAP
                placed.append(top)
                prev_bottom = top + h
            # backward: pull nodes up where there is slack
            for i in range(len(layer) - 1, -1, -1):
                nid = layer[i]
                h = size_of(nid)[1]
                want_top = desired[i] - h / 2.0
                limit = None
                if i + 1 < len(layer):
                    limit = placed[i + 1] - Y_GAP - h
                top = placed[i]
                if want_top < top:
                    top = want_top if limit is None else min(top, max(want_top, top))
                if limit is not None and top > limit:
                    top = limit
                if i > 0:
                    prev_h = size_of(layer[i - 1])[1]
                    min_top = placed[i - 1] + prev_h + Y_GAP
                    if top < min_top:
                        top = min_top
                placed[i] = top
            for nid, top in zip(layer, placed):
                center_y[nid] = top + size_of(nid)[1] / 2.0

    for _ in range(_ALIGN_PASSES):
        align_pass(True)
        align_pass(False)

    # ------------------------------------------------------------ output
    positions = {}
    for nid in ids:
        w, h = sizes[nid]
        r = rank[nid]
        positions[nid] = (col_x[r] + (col_w[r] - w) / 2.0,
                          center_y[nid] - h / 2.0)

    routes = {}
    for key, chain in routed.items():
        pts = []
        for did in chain:
            r = dummy_rank[did]
            pts.append((col_x[r] + col_w[r] / 2.0, center_y[did]))
        routes[key] = pts

    stub_pos = {}
    for key, sid in stub_node.items():
        w, h = size_of(sid)
        r = dummy_rank[sid]
        # right-align the chip to hug its consumer
        stub_pos[key] = (col_x[r] + col_w[r] - w, center_y[sid] - h / 2.0)
    return positions, warnings, routes, stubs, stub_pos


# --------------------------------------------------------------- frames
# Nested-frame layout: each marker prefix above the leaf becomes a frame,
# laid out independently then folded into its parent's layout as a single
# super-node (recursive). Cross-frame edges are drawn by the caller.

FRAME_PAD = 14.0
FRAME_TITLE_H = 18.0


class _Rep(object):
    __slots__ = ('id', 'order')

    def __init__(self, rep_id, order=None):
        self.id = rep_id
        if order is not None:
            self.order = order


class _RepEdge(object):
    __slots__ = ('src_id', 'dst_id', 'kind')

    def __init__(self, s, d, kind):
        self.src_id = s
        self.dst_id = d
        self.kind = kind


def _frame_id(path):
    return '!f!' + '\x1f'.join(path)


def compute_nested_layout(passes, resources, edges, sizes, rank_edges=None,
                          frame_pad=FRAME_PAD, frame_title=FRAME_TITLE_H):
    """Returns (positions, frame_rects, warnings).

    positions:   {node_id: (x, y)} absolute top-left per pass/resource
    frame_rects: {frame_path: (x, y, w, h)} absolute frame rectangles
    """
    warnings = []
    nodes = list(passes) + list(resources)
    if not nodes:
        return {}, {}, warnings

    container_of = {}
    for p in passes:
        container_of[p.id] = tuple(p.marker_path[:-1])
    for r in resources:
        container_of[r.id] = tuple(getattr(r, 'frame_path', ()) or ())

    frame_paths = set()
    for cpath in container_of.values():
        for level in range(1, len(cpath) + 1):
            frame_paths.add(cpath[:level])

    children_frames = {}
    members = {}
    for fp in frame_paths:
        children_frames.setdefault(fp[:-1], []).append(fp)
    for nid, cpath in container_of.items():
        members.setdefault(cpath, []).append(nid)

    def lca(a, b):
        i = 0
        while i < len(a) and i < len(b) and a[i] == b[i]:
            i += 1
        return a[:i]

    def rep_at(nid, container):
        cpath = container_of[nid]
        if cpath == container:
            return nid
        return _frame_id(cpath[:len(container) + 1])

    level_edges = {}
    for e in edges:
        ca = container_of.get(e.src_id)
        cb = container_of.get(e.dst_id)
        if ca is None or cb is None:
            continue
        level_edges.setdefault(lca(ca, cb), []).append(e)
    level_rank = {}
    for e in (rank_edges if rank_edges is not None else edges):
        ca = container_of.get(e.src_id)
        cb = container_of.get(e.dst_id)
        if ca is None or cb is None:
            continue
        level_rank.setdefault(lca(ca, cb), []).append(e)

    node_order = {}
    for p in passes:
        node_order[p.id] = p.order

    # deepest first so children are sized before parents, root last
    containers = sorted(frame_paths, key=lambda fp: (-len(fp), fp))
    containers.append(())

    frame_size = {}   # path -> (w, h) including padding + title bar
    frame_order = {}  # path -> order hint for the parent's layout
    rel_pos = {}      # rep id -> (x, y) relative to container content origin

    for container in containers:
        reps = []
        rep_sizes = {}
        for fp in sorted(children_frames.get(container, [])):
            fid = _frame_id(fp)
            reps.append(_Rep(fid, frame_order.get(fp)))
            rep_sizes[fid] = frame_size[fp]
        for nid in sorted(members.get(container, [])):
            reps.append(_Rep(nid, node_order.get(nid)))
            rep_sizes[nid] = sizes[nid]

        def map_level(edge_list):
            out = []
            for e in edge_list:
                rs = rep_at(e.src_id, container)
                rd = rep_at(e.dst_id, container)
                if rs == rd:
                    continue
                out.append(_RepEdge(rs, rd, getattr(e, 'kind', '')))
            return out

        pos, lw, _routes, _stubs, _sp = compute_layout_full(
            reps, map_level(level_edges.get(container, [])), rep_sizes,
            rank_edges=map_level(level_rank.get(container, [])) or None,
            stub_span=None)
        warnings.extend(lw)

        min_x = min(p[0] for p in pos.values())
        min_y = min(p[1] for p in pos.values())
        max_x = max(pos[r.id][0] + rep_sizes[r.id][0] for r in reps)
        max_y = max(pos[r.id][1] + rep_sizes[r.id][1] for r in reps)
        for r in reps:
            x, y = pos[r.id]
            rel_pos[r.id] = (x - min_x, y - min_y)

        content_w = max_x - min_x
        content_h = max_y - min_y
        orders = [frame_order.get(fp) for fp in children_frames.get(container, [])]
        orders += [node_order.get(nid) for nid in members.get(container, [])]
        orders = [o for o in orders if o is not None]
        if container:
            frame_size[container] = (content_w + 2 * frame_pad,
                                     content_h + 2 * frame_pad + frame_title)
            frame_order[container] = min(orders) if orders else 10 ** 9

    positions = {}
    frame_rects = {}

    def place(container, origin_x, origin_y):
        for fp in children_frames.get(container, []):
            fid = _frame_id(fp)
            rx, ry = rel_pos[fid]
            w, h = frame_size[fp]
            fx = origin_x + rx
            fy = origin_y + ry
            frame_rects[fp] = (fx, fy, w, h)
            place(fp, fx + frame_pad, fy + frame_title + frame_pad)
        for nid in members.get(container, []):
            rx, ry = rel_pos[nid]
            positions[nid] = (origin_x + rx, origin_y + ry)

    place((), 0.0, 0.0)
    return positions, frame_rects, warnings


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


def compute_layout_auto(nodes, edges, sizes, rank_edges=None):
    """Try gknv first, fall back to the built-in engine. Same return shape
    as compute_layout_full so the widget consumes either transparently."""
    warns = []
    try:
        positions, warns = compute_layout_gknv(
            nodes, edges, sizes, rank_edges=rank_edges)
        if all(n.id in positions for n in nodes):
            return positions, warns, {}, [], {}
        warns = ['gknv layout incomplete; using built-in engine']
    except Exception as exc:
        warns = ['gknv layout failed (%s); using built-in engine' % exc]
    out = compute_layout_full(nodes, edges, sizes,
                              rank_edges=rank_edges, stub_span=None)
    return (out[0], warns + list(out[1])) + tuple(out[2:])
