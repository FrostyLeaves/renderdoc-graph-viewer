# -*- coding: utf-8 -*-
"""dot-style layered layout (GKNV algorithm, graphviz dot), pure Python.

Gansner, Koutsofios, North, Vo, "A Technique for Drawing Directed
Graphs", IEEE TSE 1993. No dependencies.
graph_layout.compute_layout_gknv adapts it to the extension's contract.

Phase 1 rank: network simplex minimising weighted edge span s.t.
  rank(head)-rank(tail) >= minlen(e).
Phase 2 ordering: virtual-node chains + weighted-median sweeps + transpose.
Phase 3 coords: same simplex on the auxiliary graph for in-rank positions
  (straight chains); rank -> column packing on the other axis.

Cut values use subtree additivity: F(S) = weight leaving S minus
entering S is additive over disjoint vertex sets, so F(subtree(v)) folds
bottom-up in O(V+E) and v's parent tree edge has cut +-F(subtree(v)).
"""

import bisect
import sys
import threading

_EPS = 1e-9


class NetworkSimplex(object):
    """min sum w(e) * (rank[head] - rank[tail]) s.t.
    rank[head] - rank[tail] >= minlen(e), graph weakly connected.

    edges: (tail, head, minlen, weight). Self-edges ignored. Raises
    ValueError on cycles or a disconnected graph (caller handles both)."""

    def __init__(self, n, edges):
        self.n = n
        self.tail = []
        self.head = []
        self.minlen = []
        self.weight = []
        self.inc = [[] for _ in range(n)]   # incident edge ids per node
        for (t, h, ml, w) in edges:
            if t == h:
                continue
            eid = len(self.tail)
            self.tail.append(t)
            self.head.append(h)
            self.minlen.append(ml)
            self.weight.append(w)
            self.inc[t].append(eid)
            self.inc[h].append(eid)
        self.m = len(self.tail)
        self.rank = [0] * n
        # F({v}) = weight out of v minus into v; precomputed so subtree
        # folds never rescan incident edges
        base_f = [0] * n
        for eid in range(self.m):
            w = self.weight[eid]
            base_f[self.tail[eid]] += w
            base_f[self.head[eid]] -= w
        self.base_f = base_f
        # Tree bookkeeping (filled by solve). Pivots are INCREMENTAL (no
        # lim/low numbering): subtree membership via epoch-stamping the
        # smaller cut side, parents re-rooted only along the entering
        # path, cut values patched along the leave/enter cycle via F.
        self.tree = [False] * self.m
        self.tree_adj = None
        self.tree_edges = []       # current tree edge ids
        self.tree_pos = {}         # eid -> index in tree_edges
        self.par_edge = [-1] * n   # rooted at node 0
        self.mark = [0] * n        # epoch stamps for side membership
        self.epoch = 0
        self.cut = [0] * self.m

    def _init_rank(self):
        """Longest-path feasible ranks; ValueError on cycles. Every
        non-source node gets a tight in-edge, so the tight tree spans
        with few corrective shifts."""
        n, m = self.n, self.m
        tail, head, minlen = self.tail, self.head, self.minlen
        indeg = [0] * n
        outs = [[] for _ in range(n)]
        for eid in range(m):
            outs[tail[eid]].append(eid)
            indeg[head[eid]] += 1
        queue = [v for v in range(n) if indeg[v] == 0]
        qi = 0
        rank = self.rank
        while qi < len(queue):
            v = queue[qi]
            qi += 1
            rv = rank[v]
            for eid in outs[v]:
                h = head[eid]
                nr = rv + minlen[eid]
                if rank[h] < nr:
                    rank[h] = nr
                indeg[h] -= 1
                if indeg[h] == 0:
                    queue.append(h)
        if qi < n:
            raise ValueError('cycle in ranking graph')

    def _slack(self, eid):
        return (self.rank[self.head[eid]] - self.rank[self.tail[eid]] -
                self.minlen[eid])

    def _tight_tree(self):
        """Grow a spanning tree of tight edges from node 0, shifting the
        grown part by the minimum outside slack whenever it stalls."""
        n = self.n
        if n == 0:
            return
        tail, head, inc = self.tail, self.head, self.inc
        in_tree = [False] * n
        in_tree[0] = True
        count = 1
        tree = self.tree
        frontier = [0]
        while True:
            while frontier:
                v = frontier.pop()
                for eid in inc[v]:
                    if tree[eid]:
                        continue
                    t, h = tail[eid], head[eid]
                    o = h if t == v else t
                    if in_tree[o]:
                        continue
                    if abs(self._slack(eid)) <= _EPS:
                        tree[eid] = True
                        in_tree[o] = True
                        count += 1
                        frontier.append(o)
            if count == n:
                return
            best = None
            best_sl = None
            for eid in range(self.m):
                t, h = tail[eid], head[eid]
                if in_tree[t] == in_tree[h]:
                    continue
                sl = self._slack(eid)
                if best is None or sl < best_sl:
                    best, best_sl = eid, sl
            if best is None:
                raise ValueError('disconnected ranking graph')
            delta = -best_sl if in_tree[head[best]] else best_sl
            rank = self.rank
            for v in range(n):
                if in_tree[v]:
                    rank[v] += delta
            # chosen edge now tight; resume growth from both ends
            frontier = [v for v in range(n) if in_tree[v]]

    def _init_tree(self):
        """Build tree_adj/tree_edges, root at 0, fold F(subtree)
        bottom-up into each tree edge's cut. Runs ONCE per solve; pivots
        maintain everything incrementally."""
        n = self.n
        tail, head = self.tail, self.head
        tree_adj = [[] for _ in range(n)]
        tree_edges = []
        tree_pos = {}
        for eid in range(self.m):
            if self.tree[eid]:
                tree_adj[tail[eid]].append(eid)
                tree_adj[head[eid]].append(eid)
                tree_pos[eid] = len(tree_edges)
                tree_edges.append(eid)
        self.tree_adj = tree_adj
        self.tree_edges = tree_edges
        self.tree_pos = tree_pos

        par_edge = self.par_edge
        visited = [False] * n
        visited[0] = True
        par_edge[0] = -1
        ptr = [0] * n
        stack = [0]
        postorder = []
        while stack:
            v = stack[-1]
            adj = tree_adj[v]
            i = ptr[v]
            na = len(adj)
            child = -1
            while i < na:
                eid = adj[i]
                i += 1
                t = tail[eid]
                o = head[eid] if t == v else t
                if not visited[o]:
                    child = o
                    break
            ptr[v] = i
            if child >= 0:
                visited[child] = True
                par_edge[child] = eid
                stack.append(child)
            else:
                postorder.append(v)
                stack.pop()

        F = self.base_f[:]
        cut = self.cut
        for v in postorder:
            pe = par_edge[v]
            if pe >= 0:
                fv = F[v]
                if tail[pe] == v:
                    cut[pe] = fv
                    F[head[pe]] += fv
                else:
                    cut[pe] = -fv
                    F[tail[pe]] += fv

    def _collect_smaller_side(self, cut_eid):
        """Remove cut_eid, explore both sides in lockstep; returns
        (members, is_tail_side) of the side that finishes first. Tail
        side stamped epoch-1, head side epoch."""
        tail, head = self.tail, self.head
        tree_adj = self.tree_adj
        self.epoch += 3   # reserve epoch-1, epoch, epoch+1 (lca stamp)
        et = self.epoch - 1
        eh = self.epoch
        mark = self.mark
        t0, h0 = tail[cut_eid], head[cut_eid]
        mark[t0] = et
        mark[h0] = eh
        ft = [t0]
        fh = [h0]
        it = ih = 0
        while True:
            if it < len(ft):
                v = ft[it]
                it += 1
                for eid in tree_adj[v]:
                    if eid == cut_eid:
                        continue
                    t = tail[eid]
                    o = head[eid] if t == v else t
                    if mark[o] != et:
                        mark[o] = et
                        ft.append(o)
            else:
                return ft, True
            if ih < len(fh):
                v = fh[ih]
                ih += 1
                for eid in tree_adj[v]:
                    if eid == cut_eid:
                        continue
                    t = tail[eid]
                    o = head[eid] if t == v else t
                    if mark[o] != eh:
                        mark[o] = eh
                        fh.append(o)
            else:
                return fh, False

    def _pivot(self, leave):
        """One incremental simplex exchange:
        - smaller cut side via lockstep search (epoch stamps)
        - enter = min-slack edge INTO the tail side (cut(leave) =
          F(tail side) < 0 makes that the only profitable direction)
        - ranks shift on the smaller side (complement shift is equivalent)
        - re-root parents only along q->r0 in the moved side (not root 0's)
        - cut patched: +-F(moved side) along both root-side arms to the
          LCA, local recurrence along the re-rooted path."""
        tail, head = self.tail, self.head
        rank, minlen, tree = self.rank, self.minlen, self.tree
        mark = self.mark
        small, small_is_tail = self._collect_smaller_side(leave)
        et = self.epoch - 1
        eh = self.epoch

        # enter edge: tail outside the tail side, head inside it
        best = -1
        best_sl = None
        if small_is_tail:
            for eid in range(self.m):
                if tree[eid] or mark[head[eid]] != et or \
                        mark[tail[eid]] == et:
                    continue
                sl = rank[head[eid]] - rank[tail[eid]] - minlen[eid]
                if best_sl is None or sl < best_sl:
                    best, best_sl = eid, sl
        else:
            for eid in range(self.m):
                if tree[eid] or mark[tail[eid]] != eh or \
                        mark[head[eid]] == eh:
                    continue
                sl = rank[head[eid]] - rank[tail[eid]] - minlen[eid]
                if best_sl is None or sl < best_sl:
                    best, best_sl = eid, sl
        if best < 0:
            return False  # defensive: a negative cut implies one exists
        enter = best

        # rank shift: tail side moves by -slack
        if best_sl:
            if small_is_tail:
                for w in small:
                    rank[w] -= best_sl
            else:
                for w in small:
                    rank[w] += best_sl

        # the side without root 0 re-attaches
        root_in_tail = (mark[0] == et) if small_is_tail else \
            (mark[0] != eh)
        F_tail = self.cut[leave]
        # enter oriented INTO the tail side: tail[enter] in head side,
        # head[enter] in tail side
        if root_in_tail:
            F_moved = -F_tail
            r0 = head[leave]      # moved (head) side's old internal root
            u = tail[leave]       # root-side end of the removed edge
            q = tail[enter]       # moved-side end of the entering edge
            p = head[enter]
        else:
            F_moved = F_tail
            r0 = tail[leave]
            u = head[leave]
            q = head[enter]
            p = tail[enter]

        # structure swap
        tree[leave] = False
        tree[enter] = True
        tadj = self.tree_adj
        tadj[tail[leave]].remove(leave)
        tadj[head[leave]].remove(leave)
        tadj[tail[enter]].append(enter)
        tadj[head[enter]].append(enter)
        tree_edges, tree_pos = self.tree_edges, self.tree_pos
        pos = tree_pos.pop(leave)
        last = tree_edges.pop()
        if last != leave:
            tree_edges[pos] = last
            tree_pos[last] = pos
        tree_pos[enter] = len(tree_edges)
        tree_edges.append(enter)

        # re-root the moved side at q (flip par along q -> r0)
        par = self.par_edge
        path_edges = []
        path_nodes = [q]
        w = q
        prev = enter
        while w != r0:
            pe = par[w]
            par[w] = prev
            prev = pe
            path_edges.append(pe)
            w = head[pe] if tail[pe] == w else tail[pe]
            path_nodes.append(w)
        par[r0] = prev

        # cut maintenance
        cut = self.cut
        base_f = self.base_f
        # (a) re-rooted path: local recurrence, children-first (r0 -> q)
        for i in range(len(path_edges) - 1, -1, -1):
            e = path_edges[i]
            c = path_nodes[i + 1]   # endpoint farther from q
            f = base_f[c]
            for e2 in tadj[c]:
                if e2 == e:
                    continue
                o = head[e2] if tail[e2] == c else tail[e2]
                f += cut[e2] if tail[e2] == o else -cut[e2]
            cut[e] = f if tail[e] == c else -f
        # (b) entering edge holds the whole moved side below q
        cut[enter] = F_moved if tail[enter] == q else -F_moved
        # (c) root-side arms: u->lca lose the moved side, p->lca gain it.
        # Stamp u's ancestor chain; first stamped node ascending from p = LCA.
        ea = self.epoch + 1
        w = u
        while True:
            mark[w] = ea
            pe = par[w]
            if pe < 0:
                break
            w = head[pe] if tail[pe] == w else tail[pe]
        w = p
        while mark[w] != ea:
            pe = par[w]
            cut[pe] += F_moved if tail[pe] == w else -F_moved
            w = head[pe] if tail[pe] == w else tail[pe]
        lca = w
        w = u
        while w != lca:
            pe = par[w]
            cut[pe] += -F_moved if tail[pe] == w else F_moved
            w = head[pe] if tail[pe] == w else tail[pe]
        return True

    def _check_cuts(self):
        """Test hook: max abs deviation of incremental cut values from a
        from-scratch rebuild."""
        saved = self.cut[:]
        par_saved = self.par_edge[:]
        self._init_tree()
        worst = 0
        for eid in self.tree_edges:
            d = abs(saved[eid] - self.cut[eid])
            if d > worst:
                worst = d
        self.cut = saved
        self.par_edge = par_saved
        return worst

    def _balance_late(self):
        """ALAP consumer-hugging tie resolution: when w_in == w_out the
        move-later delta is 0, so push the node as late as its out-edges
        allow. Descending-rank order lets tie chains settle; optimum
        unchanged."""
        n = self.n
        outs = [[] for _ in range(n)]
        ins = [[] for _ in range(n)]
        for eid in range(self.m):
            outs[self.tail[eid]].append(eid)
            ins[self.head[eid]].append(eid)
        order = sorted(range(n), key=lambda v: -self.rank[v])
        for _ in range(2):
            moved = False
            for v in order:
                oeids = outs[v]
                if not oeids:
                    continue
                w_out = sum(self.weight[e] for e in oeids)
                w_in = sum(self.weight[e] for e in ins[v])
                if w_in != w_out:
                    continue
                late = min(self.rank[self.head[e]] - self.minlen[e]
                           for e in oeids)
                if late > self.rank[v]:
                    self.rank[v] = late
                    moved = True
            if not moved:
                break

    def solve(self, max_iter=None, balance_late=False, warm=None):
        """Returns ranks (min normalised to 0). max_iter caps pivots:
        feasible at any cap, optimal when the loop exits on its own.
        balance_late: consumer tie resolution (ranking phase); off for
        coordinates so symmetric ties stay put. warm: feasible starting
        ranks; near-optimal start cuts pivots several-fold (optimum
        unchanged), falls back to longest-path if infeasible."""
        n, m = self.n, self.m
        if n == 0:
            return []
        if warm is not None and len(warm) == n:
            self.rank = list(warm)
            ok = True
            rank, tail, head, minlen = (self.rank, self.tail, self.head,
                                        self.minlen)
            for eid in range(m):
                if rank[head[eid]] - rank[tail[eid]] - minlen[eid] < -_EPS:
                    ok = False
                    break
            if not ok:
                self._init_rank()
        else:
            self._init_rank()
        if m == 0:
            return [0] * n
        self._tight_tree()
        self._init_tree()
        if max_iter is None:
            max_iter = 4 * m + 100
        start = 0
        it = 0
        cut = self.cut
        while it < max_iter:
            tree_edges = self.tree_edges
            nt = len(tree_edges)
            leave = -1
            for k in range(nt):
                eid = tree_edges[(start + k) % nt]
                if cut[eid] < -_EPS:
                    leave = eid
                    start = (start + k + 1) % nt
                    break
            if leave < 0:
                break
            if not self._pivot(leave):
                break
            it += 1
        if balance_late:
            self._balance_late()
        lo = min(self.rank)
        if lo:
            self.rank = [r - lo for r in self.rank]
        return self.rank


def solve_ranks(n, edges, max_iter=None, balance_late=True):
    """NetworkSimplex per weakly-connected component; isolated nodes get
    rank 0. balance_late defaults on for this ranking-phase entry point."""
    if n == 0:
        return []
    adj = [[] for _ in range(n)]
    for (t, h, _ml, _w) in edges:
        if t != h:
            adj[t].append(h)
            adj[h].append(t)
    comp = [-1] * n
    comps = []
    for v in range(n):
        if comp[v] >= 0:
            continue
        cid = len(comps)
        members = [v]
        comp[v] = cid
        stack = [v]
        while stack:
            a = stack.pop()
            for b in adj[a]:
                if comp[b] < 0:
                    comp[b] = cid
                    members.append(b)
                    stack.append(b)
        comps.append(members)

    ranks = [0] * n
    for members in comps:
        if len(members) == 1:
            continue
        local = dict((v, i) for i, v in enumerate(members))
        sub = [(local[t], local[h], ml, w) for (t, h, ml, w) in edges
               if t in local and h in local and t != h]
        ns = NetworkSimplex(len(members), sub)
        sub_ranks = ns.solve(max_iter=max_iter, balance_late=balance_late)
        for v, i in local.items():
            ranks[v] = sub_ranks[i]
    return ranks


# ===================================================================== #
# Phase 2 ordering + Phase 3 coordinates                                 #
# ===================================================================== #

_ORDER_ITER = 8
_VIRTUAL_H = 1.0

# Auxiliary-graph straightening priorities: virtual-virtual (long-edge
# interior) pulls hardest so routed edges run straight; real endpoints
# bend more freely.
_OMEGA_RR = 1
_OMEGA_RV = 2
_OMEGA_VV = 8


def _median_value(positions):
    """GKNV weighted median of neighbor positions; None if no neighbors."""
    m = len(positions)
    if m == 0:
        return None
    P = sorted(positions)
    half = m // 2
    if m % 2 == 1:
        return float(P[half])
    if m == 2:
        return (P[0] + P[1]) / 2.0
    left = float(P[half - 1] - P[0])
    right = float(P[-1] - P[half])
    if left + right == 0:
        return (P[half - 1] + P[half]) / 2.0
    return (P[half - 1] * right + P[half] * left) / (left + right)


def _count_crossings(layers, pos, seg_out):
    """Crossings over all adjacent layer pairs: sort segments by tail
    position, count inversions of head positions."""
    total = 0
    for li in range(len(layers) - 1):
        pts = []
        for u in layers[li]:
            for v in seg_out.get(u, ()):
                pts.append((pos[u], pos[v]))
        pts.sort()
        seq = [p[1] for p in pts]
        # count inversions by insertion (layers are small)
        seen = []
        for x in seq:
            i = bisect.bisect_right(seen, x)
            total += len(seen) - i
            seen.insert(i, x)
    return total


class _Component(object):
    """One component readied for ordering/coordinates. Node keys: caller
    ids for real nodes, ('~', i) tuples for virtual nodes."""

    def __init__(self, ranks, segments, heights, widths, real_ids):
        self.ranks = ranks          # key -> dense rank
        self.segments = segments    # (u, v, omega), rank[v] = rank[u]+1
        self.heights = heights      # key -> height (virtuals small)
        self.widths = widths        # key -> width
        self.real_ids = real_ids    # caller ids only


def _build_component(ids, rank, edges):
    """Densify ranks, split long edges into virtual chains. edges:
    deduped (src, dst) pairs in this component."""
    distinct = sorted(set(rank[i] for i in ids))
    dense = dict((r, i) for i, r in enumerate(distinct))
    ranks = dict((i, dense[rank[i]]) for i in ids)

    segments = []
    heights = {}
    widths = {}
    vcount = [0]

    def new_virtual(r):
        key = ('~', vcount[0])
        vcount[0] += 1
        ranks[key] = r
        heights[key] = _VIRTUAL_H
        widths[key] = _VIRTUAL_H
        return key

    for (u, v) in edges:
        span = ranks[v] - ranks[u]
        if span <= 0:
            continue            # flat/backward edge: drawn directly
        if span == 1:
            segments.append((u, v, _OMEGA_RR))
            continue
        prev = u
        for r in range(ranks[u] + 1, ranks[v]):
            d = new_virtual(r)
            segments.append((prev, d,
                             _OMEGA_RV if prev == u else _OMEGA_VV))
            prev = d
        segments.append((prev, v, _OMEGA_RV))
    return _Component(ranks, segments, heights, widths, list(ids))


def _order_component(comp, hint):
    """Returns crossing-minimised layers (list of rank lists). DFS init
    in hint order, then weighted-median sweeps + transpose, keeping the
    best ordering seen."""
    seg_out = {}
    seg_in = {}
    for (u, v, _w) in comp.segments:
        seg_out.setdefault(u, []).append(v)
        seg_in.setdefault(v, []).append(u)

    def hint_key(k):
        h = hint.get(k)
        return ((0, h, str(k)) if h is not None else (1, 0, str(k)))

    nrank = max(comp.ranks.values()) + 1 if comp.ranks else 0
    layers = [[] for _ in range(nrank)]
    placed = set()

    roots = [k for k in comp.ranks if not seg_in.get(k)]
    roots.sort(key=hint_key)
    stack = list(reversed(roots))
    while stack:
        k = stack.pop()
        if k in placed:
            continue
        placed.add(k)
        layers[comp.ranks[k]].append(k)
        kids = sorted(seg_out.get(k, ()), key=hint_key, reverse=True)
        stack.extend(kids)
    leftovers = sorted((k for k in comp.ranks if k not in placed),
                       key=hint_key)
    for k in leftovers:
        layers[comp.ranks[k]].append(k)

    pos = {}
    for layer in layers:
        for i, k in enumerate(layer):
            pos[k] = i

    def wmedian(forward):
        seq = range(nrank) if forward else range(nrank - 1, -1, -1)
        adj = seg_in if forward else seg_out
        for r in seq:
            layer = layers[r]
            decorated = []
            for i, k in enumerate(layer):
                med = _median_value([pos[o] for o in adj.get(k, ())])
                decorated.append((pos[k] if med is None else med, i, k))
            decorated.sort()
            layers[r] = [k for (_m, _i, k) in decorated]
            for i, k in enumerate(layers[r]):
                pos[k] = i

    def pair_cross(a, b):
        c = 0
        for adj in (seg_in, seg_out):
            pa = sorted(pos[o] for o in adj.get(a, ()))
            pb = sorted(pos[o] for o in adj.get(b, ()))
            for x in pa:
                c += bisect.bisect_left(pb, x)
        return c

    def transpose():
        for _round in range(4):
            improved = False
            for r in range(nrank):
                layer = layers[r]
                for i in range(len(layer) - 1):
                    a, b = layer[i], layer[i + 1]
                    if pair_cross(a, b) > pair_cross(b, a):
                        layer[i], layer[i + 1] = b, a
                        pos[a], pos[b] = pos[b], pos[a]
                        improved = True
            if not improved:
                return

    best_layers = [list(l) for l in layers]
    best_cross = _count_crossings(layers, pos, seg_out)
    for it in range(_ORDER_ITER):
        wmedian(it % 2 == 0)
        transpose()
        c = _count_crossings(layers, pos, seg_out)
        if c < best_cross:
            best_cross = c
            best_layers = [list(l) for l in layers]
        if best_cross == 0:
            break
    for r, layer in enumerate(best_layers):
        for i, k in enumerate(layer):
            pos[k] = i
    return best_layers


def _warm_centers(comp, layers, node_gap):
    """Weighted-median alignment: cheap near-optimal feasible start for
    the coordinate simplex. Neighbour pulls carry the segment's omega so
    virtual chains (omega 8) dominate as in the simplex objective; an
    unweighted start would leave hundreds of straightening pivots.
    Returns {key: center_y} respecting in-layer separations."""
    nb = {}
    for (u, v, w) in comp.segments:
        nb.setdefault(u, []).append((v, w))
        nb.setdefault(v, []).append((u, w))
    heights = comp.heights
    centers = {}
    for layer in layers:
        cum = 0.0
        for k in layer:
            h = heights[k]
            centers[k] = cum + h / 2.0
            cum += h + node_gap

    def weighted_median(pairs):
        # median of neighbour centers, each counted omega times
        items = sorted((centers[o], w) for (o, w) in pairs)
        total = sum(w for (_c, w) in items)
        acc = 0
        for (c, w) in items:
            acc += 2 * w
            if acc >= total:
                return c
        return items[-1][0]

    for _ in range(4):
        for forward in (True, False):
            seq = layers if forward else list(reversed(layers))
            for layer in seq:
                desired = []
                for k in layer:
                    vs = nb.get(k)
                    if vs:
                        desired.append(weighted_median(vs))
                    else:
                        desired.append(centers[k])
                placed = []
                prev_bot = None
                for k, want in zip(layer, desired):
                    h = heights[k]
                    top = want - h / 2.0
                    if prev_bot is not None and top < prev_bot + node_gap:
                        top = prev_bot + node_gap
                    placed.append(top)
                    prev_bot = top + h
                for i in range(len(layer) - 1, -1, -1):
                    k = layer[i]
                    h = heights[k]
                    want_top = desired[i] - h / 2.0
                    top = placed[i]
                    if want_top < top:
                        nt = want_top
                        if i > 0:
                            mn = (placed[i - 1] +
                                  heights[layer[i - 1]] + node_gap)
                            if nt < mn:
                                nt = mn
                        if i + 1 < len(layer):
                            mx = placed[i + 1] - node_gap - h
                            if nt > mx:
                                nt = mx
                        placed[i] = nt
                    centers[k] = placed[i] + h / 2.0
    return centers


def _coords_component(comp, layers, node_gap):
    """GKNV phase 3: in-rank centre coords via network simplex on the
    auxiliary graph. Aux nodes = component nodes + one per segment; a
    segment node -> both endpoints (minlen 0, weight omega) makes the
    optimum minimise weighted |y(u) - y(v)|; consecutive in-rank pairs
    get zero-weight separation edges (heights + node_gap). Warm-started
    from the median heuristic (cuts pivots, optimum unchanged)."""
    keys = []
    for layer in layers:
        keys.extend(layer)
    index = dict((k, i) for i, k in enumerate(keys))
    nbase = len(keys)

    aux_edges = []
    for si, (u, v, omega) in enumerate(comp.segments):
        s = nbase + si
        aux_edges.append((s, index[u], 0.0, omega))
        aux_edges.append((s, index[v], 0.0, omega))
    n_aux = nbase + len(comp.segments)

    for layer in layers:
        for i in range(len(layer) - 1):
            a, b = layer[i], layer[i + 1]
            sep = (comp.heights[a] + comp.heights[b]) / 2.0 + node_gap
            aux_edges.append((index[a], index[b], sep, 0))

    warm_c = _warm_centers(comp, layers, node_gap)
    warm = [0.0] * n_aux
    for k, i in index.items():
        warm[i] = warm_c[k]
    for si, (u, v, _w) in enumerate(comp.segments):
        a, b = warm_c[u], warm_c[v]
        warm[nbase + si] = a if a < b else b

    ns = NetworkSimplex(n_aux, aux_edges)
    ys = ns.solve(warm=warm)
    centers = dict((k, float(ys[index[k]])) for k in keys)

    # loner gravity (parity with the built-in engine): segment-less
    # nodes cost nothing wherever they sit, so pull each toward the
    # column's connected median within its neighbours' slack.
    seg_touched = set()
    for (u, v, _w) in comp.segments:
        seg_touched.add(u)
        seg_touched.add(v)
    for layer in layers:
        anchored = [centers[k] for k in layer if k in seg_touched]
        if not anchored or len(anchored) == len(layer):
            continue
        anchored.sort()
        med = anchored[len(anchored) // 2]
        for i, k in enumerate(layer):
            if k in seg_touched:
                continue
            lo = None
            hi = None
            if i > 0:
                p = layer[i - 1]
                lo = (centers[p] + comp.heights[p] / 2.0 + node_gap +
                      comp.heights[k] / 2.0)
            if i + 1 < len(layer):
                nx = layer[i + 1]
                hi = (centers[nx] - comp.heights[nx] / 2.0 - node_gap -
                      comp.heights[k] / 2.0)
            want = med
            if lo is not None and want < lo:
                want = lo
            if hi is not None and want > hi:
                want = hi
            centers[k] = want
    return centers


def compute(ids, sizes, edges, rank_pairs=None, col_gap=120.0,
            node_gap=28.0, order_hint=None):
    """Full dot-style layout.

    ids:        node ids, stable order
    sizes:      {id: (w, h)}
    edges:      (src_id, dst_id) drawn pairs (dedup inside)
    rank_pairs: optional (src_id, dst_id) pairs used for ranking only
    order_hint: {id: sortable} initial-order hint (execution order)

    Returns ({id: (top_left_x, top_left_y)}, warnings). Components are
    laid out independently and stacked vertically.

    qrenderdoc runs extensions under a C-level line trace (~8x slower)
    that python can't restore once dropped. New threads start untraced,
    so under an active trace the work runs on a short-lived worker thread
    joined synchronously: same blocking, full speed, no host effects."""
    if sys.gettrace() is not None:
        box = {}

        def _run():
            try:
                box['r'] = _compute_impl(ids, sizes, edges, rank_pairs,
                                         col_gap, node_gap, order_hint)
            except BaseException as exc:  # re-raised on the caller thread
                box['e'] = exc

        t = threading.Thread(target=_run, name='gknv-layout')
        t.start()
        t.join()
        if 'e' in box:
            raise box['e']
        return box['r']
    return _compute_impl(ids, sizes, edges, rank_pairs, col_gap,
                         node_gap, order_hint)


def _compute_impl(ids, sizes, edges, rank_pairs, col_gap, node_gap,
                  order_hint):
    warnings = []
    idset = set(ids)
    if not ids:
        return {}, warnings
    hint = dict(order_hint or {})

    def dedup(pairs):
        seen = set()
        out = []
        for (a, b) in pairs:
            if a == b or a not in idset or b not in idset:
                continue
            if (a, b) in seen:
                continue
            seen.add((a, b))
            out.append((a, b))
        return out

    draw_pairs = dedup((e[0], e[1]) for e in edges)
    rp = dedup(rank_pairs) if rank_pairs is not None else draw_pairs

    index = dict((nid, i) for i, nid in enumerate(ids))
    redges = [(index[a], index[b], 1, 1) for (a, b) in rp]
    try:
        rank_arr = solve_ranks(len(ids), redges, balance_late=True)
    except ValueError:
        # cycle: drop DFS back edges and retry once
        warnings.append('gknv: cycle in ranking graph - back edges '
                        'ignored for layering')
        order = {}
        state = {}
        back = set()

        def dfs(start):
            stack = [(start, iter(adj.get(start, ())))]
            state[start] = 1
            while stack:
                v, it = stack[-1]
                pushed = False
                for w in it:
                    if state.get(w, 0) == 0:
                        state[w] = 1
                        stack.append((w, iter(adj.get(w, ()))))
                        pushed = True
                        break
                    elif state.get(w) == 1:
                        back.add((v, w))
                if not pushed:
                    state[v] = 2
                    stack.pop()

        adj = {}
        for (a, b) in rp:
            adj.setdefault(a, []).append(b)
        for nid in ids:
            if state.get(nid, 0) == 0:
                dfs(nid)
        rp = [(a, b) for (a, b) in rp if (a, b) not in back]
        redges = [(index[a], index[b], 1, 1) for (a, b) in rp]
        rank_arr = solve_ranks(len(ids), redges, balance_late=True)
    rank = dict((nid, rank_arr[index[nid]]) for nid in ids)

    # weakly-connected components over draw + rank pairs (keeps ranking
    # and ordering phases consistent)
    union = draw_pairs + [p for p in rp if p not in set(draw_pairs)]
    nbr = dict((nid, []) for nid in ids)
    for (a, b) in union:
        nbr[a].append(b)
        nbr[b].append(a)
    comp_of = {}
    comps = []
    for nid in ids:
        if nid in comp_of:
            continue
        cid = len(comps)
        members = [nid]
        comp_of[nid] = cid
        stack = [nid]
        while stack:
            a = stack.pop()
            for b in nbr[a]:
                if b not in comp_of:
                    comp_of[b] = cid
                    members.append(b)
                    stack.append(b)
        comps.append(members)

    positions = {}
    y_off = 0.0
    for members in comps:
        mset = set(members)
        cedges = [(a, b) for (a, b) in draw_pairs
                  if a in mset and b in mset]
        comp = _build_component(members, rank, cedges)
        for nid in members:
            w, h = sizes[nid]
            comp.widths[nid] = w
            comp.heights[nid] = h
        layers = _order_component(comp, hint)
        centers = _coords_component(comp, layers, node_gap)

        # rank -> column packing on X (real node widths only)
        ncols = len(layers)
        col_w = [0.0] * ncols
        for layer_i, layer in enumerate(layers):
            for k in layer:
                if k in mset:
                    if comp.widths[k] > col_w[layer_i]:
                        col_w[layer_i] = comp.widths[k]
        col_x = [0.0] * ncols
        x = 0.0
        for i in range(ncols):
            col_x[i] = x
            x += col_w[i] + col_gap

        ys = [centers[nid] - sizes[nid][1] / 2.0 for nid in members]
        min_y = min(ys) if ys else 0.0
        max_y2 = max(centers[nid] + sizes[nid][1] / 2.0
                     for nid in members) if members else 0.0
        for nid in members:
            w, h = sizes[nid]
            r = comp.ranks[nid]
            positions[nid] = (col_x[r] + (col_w[r] - w) / 2.0,
                              centers[nid] - h / 2.0 - min_y + y_off)
        y_off += (max_y2 - min_y) + 120.0
    return positions, warnings
