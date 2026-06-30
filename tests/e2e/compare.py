# -*- coding: utf-8 -*-
"""Canonicalize a FrameGraph into name-keyed node/edge sets and diff two of them.

Shared by the host oracle (refmodel.py) and the qrenderdoc verifier (verify_all.py)
so both canonicalize identically. Pure stdlib + FrameGraph attribute reads -- no
renderdoc, importable in either interpreter (Python 3.6). Keys drop ids/eids/order/
usage-event lists and keep names, categories, res_kind, versions, edge
direction+kind+unused, bundle membership, and portal role+target -- so a parsed
graph and a YAML-derived graph match iff they are structurally isomorphic."""


def _runs(leaves, path):
    """Contiguous instance runs (first_eid, last_eid) of a marker prefix in leaf
    order -- mirrors graph_model._leaf_runs, kept here so both the host oracle and
    the qrenderdoc verifier compute the same instance ordinals without importing
    graph_model into this shared module."""
    k = len(path)
    path = tuple(path)
    runs = []
    cur = None
    for leaf in leaves:
        if tuple(leaf.marker_path[:k]) == path:
            if cur is None:
                cur = [leaf.eid, leaf.eid]
            else:
                cur[1] = leaf.eid
        elif cur is not None:
            runs.append((cur[0], cur[1]))
            cur = None
    if cur is not None:
        runs.append((cur[0], cur[1]))
    return runs


def instance_key(leaves, path, rng):
    """Stable key for one scope instance: 'a/b#<ordinal>'."""
    if not path:
        return ''
    runs = _runs(leaves, path)
    for i, (a, b) in enumerate(runs):
        if a <= rng[0] and rng[1] <= b:
            return '%s#%d' % ('/'.join(path), i)
    return '%s#0' % '/'.join(path)


def _pass_key(p):
    role = getattr(p, 'portal_role', None)
    if role:
        return 'portal|%s|%s' % (role, p.name)
    members = getattr(p, 'bundle_members', None)
    if members:
        return 'passbundle|%s|%s' % (p.kind, ','.join(sorted(members)))
    return 'pass|%s|%s' % (p.kind, p.name)


def _res_key(r):
    members = getattr(r, 'bundle_members', None)
    if members:
        gens = getattr(r, 'generations', 1)
        return 'resbundle|%s|%s|%d' % (r.res_kind, ','.join(sorted(members)), gens)
    return 'res|%s|%s|%d' % (r.res_kind, r.name, r.version)


def canon(fg):
    """FrameGraph -> (set(node_keys), set(edge_keys))."""
    nodes = set()
    edges = set()
    id_key = {}
    for p in fg.passes:
        k = _pass_key(p)
        id_key[p.id] = k
        nodes.add(k)
    for r in fg.resources:
        k = _res_key(r)
        id_key[r.id] = k
        nodes.add(k)
    for e in fg.edges:
        s = id_key.get(e.src_id)
        d = id_key.get(e.dst_id)
        if s is None or d is None:
            continue
        unused = 1 if getattr(e, 'unused_binding', False) else 0
        edges.add('edge|%s|%s|%s|%d' % (s, d, e.kind, unused))
    return nodes, edges


def diff(exp_nodes, exp_edges, act_nodes, act_edges):
    """Symmetric difference, as sorted lists for readable reporting."""
    return {
        'missing_nodes': sorted(set(exp_nodes) - set(act_nodes)),
        'extra_nodes': sorted(set(act_nodes) - set(exp_nodes)),
        'missing_edges': sorted(set(exp_edges) - set(act_edges)),
        'extra_edges': sorted(set(act_edges) - set(exp_edges)),
    }


def is_clean(d):
    return not (d['missing_nodes'] or d['extra_nodes'] or
                d['missing_edges'] or d['extra_edges'])


def format_diff(d):
    lines = []
    for label, key in (('missing node', 'missing_nodes'),
                       ('extra node', 'extra_nodes'),
                       ('missing edge', 'missing_edges'),
                       ('extra edge', 'extra_edges')):
        for item in d[key]:
            lines.append('    %-12s %s' % (label, item))
    return '\n'.join(lines)
