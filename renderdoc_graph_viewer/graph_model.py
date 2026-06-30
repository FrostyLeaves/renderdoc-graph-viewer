# -*- coding: utf-8 -*-
"""Frame-graph extraction. Everything below the `runtime` divider touches the
renderdoc module (imported lazily, replay thread); the rest is pure Python.
"""

import bisect
import re
import time

from .i18n import tr

from . import config as _config
from .parse.usage_access import READ, WRITE, RW, IGNORE, USAGE_ACCESS
from .parse import usage_access
from .parse import usage_cleanup
from .parse import depth_access
from .parse import shader_refinement
from .parse import apis
from .parse import action_flags

# Names matching this are raw-API-call groupings (vkCmd...(...)), not semantic markers.
_API_CALL_RE = re.compile(r'^(vk|gl|wgl|egl)[A-Za-z0-9_]*\(')
_STRUCTURAL_NAME_PREFIXES = (
    'ExecuteCommandList', 'Command Buffer', 'CommandBuffer', 'API Calls',
)

EDGE_RANK = 'rank'     # Edge.kind for a layout-only ordering edge (besides READ / WRITE)

# Action kinds (LeafAction.kind).
KIND_DRAW = 'draw'
KIND_DISPATCH = 'dispatch'
KIND_CLEAR = 'clear'
KIND_TRANSFER = 'transfer'
KIND_PRESENT = 'present'

# Pass categories (PassNode.kind); also the colour keys consumed by graph_widget.
CAT_GRAPHICS = 'graphics'
CAT_COMPUTE = 'compute'
CAT_TRANSFER = 'transfer'
CAT_PRESENT = 'present'
CAT_SCOPE = 'scope'
CAT_PORTAL = 'portal'

# Resource kinds (ResourceVersionNode.res_kind); also colour keys.
RES_COLOR = 'color'
RES_DEPTH = 'depth'
RES_UAV_TEX = 'uav_tex'
RES_SWAPCHAIN = 'swapchain'
RES_BUFFER = 'buffer'
RES_SAMPLED = 'sampled'

# Portal roles (PortalNode.role).
ROLE_PRODUCER = 'producer'
ROLE_CONSUMER = 'consumer'

# Graph node types.
NODE_PASS = 'pass'
NODE_RESOURCE = 'resource'
NODE_PORTAL = 'portal'

class LeafAction(object):
    """One executable action flattened out of the action tree (IR)."""

    __slots__ = ('eid', 'kind', 'group_outputs', 'group_depth', 'marker_path',
                 'name', 'copy_src_hint', 'copy_dst_hint')

    def __init__(self, eid, kind, group_outputs=(), group_depth=None,
                 marker_path=(), name='', copy_src_hint=None,
                 copy_dst_hint=None):
        self.eid = eid
        self.kind = kind  # 'draw' | 'dispatch' | 'clear' | 'transfer' | 'present'
        # Resource hints for pass grouping/naming. Present source hints are
        # normalised into usage_by_res during extraction.
        self.group_outputs = tuple(o for o in group_outputs if o is not None)
        self.group_depth = group_depth
        self.marker_path = tuple(marker_path)
        self.name = name
        self.copy_src_hint = copy_src_hint
        self.copy_dst_hint = copy_dst_hint

    def draw_targets(self):
        """Render-target set written by a draw or clear: colour outputs plus
        the depth target."""
        t = set(self.group_outputs)
        if self.group_depth is not None:
            t.add(self.group_depth)
        return t

    def targets(self):
        # a clear additionally counts its copy destination as a target
        t = self.draw_targets()
        if self.copy_dst_hint is not None:
            t.add(self.copy_dst_hint)
        return t


class PassNode(object):
    def __init__(self, order, kind, name, leaves, marker_path,
                 collapsed_frame=False):
        self.node_type = NODE_PASS
        self.id = 'p%d' % order
        self.order = order
        self.kind = kind  # 'graphics' | 'compute' | 'transfer' | 'present'
        self.name = name
        self.leaves = list(leaves)
        self.first_eid = min(l.eid for l in self.leaves)
        self.last_eid = max(l.eid for l in self.leaves)
        self.action_count = len(self.leaves)
        self.marker_path = tuple(marker_path)
        self.collapsed_frame = collapsed_frame  # double-click expands back to children
        self.drillable = False  # double-click drills into this instance
        self.bundle_members = None
        self.bundle_member_eids = []

    @property
    def frame_path(self):
        return self.marker_path[:-1]


class ResourceVersionNode(object):
    def __init__(self, res_key, name, version, res_kind, info, imported=False):
        self.node_type = NODE_RESOURCE
        self.id = 'r%s_v%d' % (res_key, version)
        self.res_key = res_key
        self.name = name
        self.version = version
        # total write-version count for this res_key, stamped by build_graph once
        # all versions exist. Drives the #n badge / (vN) label: shown when >= 2.
        self.version_count = version
        self.res_kind = res_kind  # 'color' | 'depth' | 'uav_tex' | 'swapchain' | 'buffer'
        self.info = dict(info or {})
        self.imported = imported
        self.last_write_eid = None
        self.first_read_eid = None
        self.writer_ids = []
        self.reader_ids = []
        self.frame_path = ()  # lowest common frame of all touchers
        self.outside_readers = 0   # frame activity beyond the current scope
        self.outside_writers = 0
        self.outside_write_eids = frozenset()
        self.scope_input = False   # written outside the scope, read inside
        self.internal = False      # content never leaves its single toucher
        self.bundle_members = None
        self.bundle_member_keys = []
        self.generations = 1

    def label(self):
        if self.version_count >= 2:
            return '%s (v%d)' % (self.name, self.version)
        return self.name


class Edge(object):
    def __init__(self, src_id, dst_id, kind):
        self.src_id = src_id
        self.dst_id = dst_id
        self.kind = kind  # 'read' | 'write'
        self.usages = []  # [(eid, usage_name)]
        # bound but never referenced by the shader at any of this edge's events
        self.unused_binding = False


class FrameGraph(object):
    def __init__(self):
        self.passes = []
        self.resources = []
        self.edges = []
        self.rank_edges = []       # DAG-safe edge set for layout ranking only
        self.orphan_pass_ids = set()  # passes with no candidate-resource I/O (UI hides)
        self.frame_paths = set()   # all marker prefixes that act as frames
        self.warnings = []
        self.stats = {}
        self.rid_objects = {}  # res_key -> renderdoc ResourceId (runtime only)

    def nodes(self):
        return list(self.passes) + list(self.resources)


def _fine_groups(leaves):
    """Markerless grouping: consecutive draws sharing group output/depth hints
    merge; consecutive dispatches merge; clears/transfers stand alone."""
    groups = []
    for act in leaves:
        key = None
        if act.kind == KIND_DRAW:
            key = (KIND_DRAW, act.group_outputs, act.group_depth)
        elif act.kind == KIND_DISPATCH:
            key = (KIND_DISPATCH,)
        if key is not None and groups and groups[-1]['key'] == key:
            groups[-1]['actions'].append(act)
        else:
            groups.append({'key': key, 'kind': act.kind, 'actions': [act],
                           'pre': [], 'name_hint': None, 'mpath': ()})
    return groups


def build_passes(leaves, res_names=None, marker_depth=None, collapsed=None,
                 scope_level=None):
    """Group leaf actions into pass nodes by semantic-marker depth.

    marker_depth=N: depth N is the leaf level (one node per consecutive
    depth-N marker run, deeper activity folds into its depth-N ancestor);
    None = group by full marker path. Shallower markers become nested frames.

    collapsed: frame paths whose leaves aggregate into one collapsed_frame
    node (shallowest collapsed ancestor wins). Markerless leaves fall back to
    the fine rules; present actions never aggregate.
    """
    res_names = res_names or {}
    collapsed = set(collapsed or ())

    def trunc(path):
        p = tuple(path)
        if scope_level is not None:
            # focus nav: one level below scope; leaves at the scope path use fine rules
            if len(p) <= scope_level:
                return (), False
            return p[:scope_level + 1], False
        for level in range(1, len(p) + 1):
            if p[:level] in collapsed:
                return p[:level], True
        if marker_depth is None:
            return p, False
        return p[:marker_depth], False

    runs = []  # (key, is_collapsed, [leaves]); key None marks a present (never merges)
    for act in leaves:
        if act.kind == KIND_PRESENT:
            runs.append((None, False, [act]))
            continue
        key, is_col = trunc(act.marker_path)
        if runs and runs[-1][0] is not None and runs[-1][0] == key:
            runs[-1][2].append(act)
        else:
            runs.append((key, is_col, [act]))

    groups = []
    for key, is_col, acts in runs:
        if key is None:  # present
            groups.append({'key': None, 'kind': KIND_PRESENT, 'actions': acts,
                           'pre': [], 'name_hint': None, 'collapsed': False,
                           'mpath': tuple(acts[0].marker_path)})
        elif len(key) > 0:  # marker-aggregated node
            kinds = set(a.kind for a in acts)
            if KIND_DRAW in kinds:
                kind = KIND_DRAW
            elif KIND_DISPATCH in kinds:
                kind = KIND_DISPATCH
            elif kinds == set([KIND_CLEAR]):
                kind = KIND_CLEAR
            else:
                kind = KIND_TRANSFER
            groups.append({'key': key, 'kind': kind, 'actions': acts,
                           'pre': [], 'name_hint': key[-1],
                           'collapsed': is_col, 'mpath': key})
        else:  # markerless run
            groups.extend(_fine_groups(acts))

    def first_draw_targets(g):
        for a in g['actions']:
            if a.kind == KIND_DRAW:
                return a.draw_targets()
        return set()

    # fold unnamed standalone clears into the following draw-bearing group
    folded = []
    i = 0
    while i < len(groups):
        g = groups[i]
        if g['kind'] != KIND_CLEAR or g['name_hint'] is not None:
            folded.append(g)
            i += 1
            continue
        run = []
        while (i < len(groups) and groups[i]['kind'] == KIND_CLEAR and
               groups[i]['name_hint'] is None):
            run.append(groups[i])
            i += 1
        nxt = None
        nxt_targets = set()
        if i < len(groups) and groups[i]['kind'] == KIND_DRAW:
            nxt = groups[i]
            nxt_targets = first_draw_targets(nxt)
        for cg in run:
            clr = cg['actions'][0]
            tgts = clr.targets()
            if nxt is not None and tgts and tgts <= nxt_targets:
                nxt['pre'].append(clr)
            else:
                folded.append(cg)

    kind_map = {KIND_DRAW: CAT_GRAPHICS, KIND_DISPATCH: CAT_COMPUTE,
                KIND_CLEAR: CAT_TRANSFER, KIND_TRANSFER: CAT_TRANSFER,
                KIND_PRESENT: CAT_PRESENT}
    passes = []
    name_counts = {}
    for order, g in enumerate(folded):
        acts = list(g['pre']) + list(g['actions'])
        primary = g['actions'][0]
        base = g['name_hint'] or ''
        if not base:
            if g['kind'] == KIND_PRESENT:
                base = 'Present'
            elif g['kind'] == KIND_DRAW:
                rt = (primary.group_outputs[0] if primary.group_outputs
                      else primary.group_depth)
                rtname = res_names.get(rt, str(rt)) if rt is not None else 'No RT'
                base = 'Pass #%d (%s)' % (order + 1, rtname)
            elif g['kind'] == KIND_DISPATCH:
                base = 'Compute #%d' % (order + 1)
            else:
                base = primary.name or 'Transfer'
        n = name_counts.get(base, 0) + 1
        name_counts[base] = n
        name = base if n == 1 else '%s #%d' % (base, n)
        passes.append(PassNode(order, kind_map[g['kind']], name, acts,
                               g['mpath'],
                               collapsed_frame=g.get('collapsed', False)))
    return passes


def _bucket_usages(passes, usage_by_res, boundaries=None):
    """Bucket usage events into pass intervals.

    Gap events use renderpass boundary direction when available, then the
    usage-name heuristic, then nearest pass. Events outside all passes drop.

    -> {res_key: {pass_index: {'r': [(eid, uname)], 'w': [(eid, uname)]}}}
    """
    boundaries = boundaries or {}
    firsts = [p.first_eid for p in passes]

    def find_pass(eid, uname):
        i = bisect.bisect_right(firsts, eid) - 1
        if 0 <= i and passes[i].first_eid <= eid <= passes[i].last_eid:
            return i
        if 0 <= i and i + 1 < len(passes):
            bdir = boundaries.get(eid)
            if bdir == 'end':
                return i
            if bdir == 'begin':
                return i + 1
            if uname in ('Clear', 'Discard'):
                return i + 1
            if uname in ('ColorTarget', 'DepthStencilTarget',
                         'ResolveSrc', 'ResolveDst'):
                return i
            prev_d = eid - passes[i].last_eid
            next_d = passes[i + 1].first_eid - eid
            return i + 1 if next_d <= prev_d else i
        return None

    slots = {}
    for res_key in sorted(usage_by_res.keys()):
        per_pass = {}
        for eid, uname in sorted(usage_by_res[res_key]):
            acc = usage_access.direction(uname)
            if acc == IGNORE:
                continue
            pi = find_pass(eid, uname)
            if pi is None:
                continue
            slot = per_pass.setdefault(pi, {'r': [], 'w': []})
            if acc in (READ, RW):
                slot['r'].append((eid, uname))
            if acc in (WRITE, RW):
                slot['w'].append((eid, uname))
        if per_pass:
            slots[res_key] = per_pass
    return slots


def _stamp_version_counts(fg, version_count):
    """Stamp each resource node with its res_key's total write-version count,
    which drives the #n badge / (vN) label."""
    for node in fg.resources:
        node.version_count = version_count.get(node.res_key, node.version)


def _detect_orphans(fg):
    """Flag passes with no candidate-resource I/O (the UI hides them) and give
    each a rank hint after the nearest preceding non-orphan so it stays placed
    when shown."""
    incident = set()
    for e in fg.edges:
        incident.add(e.src_id)
        incident.add(e.dst_id)
    fg.orphan_pass_ids = set(p.id for p in fg.passes if p.id not in incident)
    last_toucher = None
    for p in fg.passes:
        if p.id in fg.orphan_pass_ids:
            if last_toucher is not None:
                fg.rank_edges.append(Edge(last_toucher.id, p.id, EDGE_RANK))
        else:
            last_toucher = p


def _assign_frame_paths(fg):
    """Record every nested frame path and set each resource's lowest common
    frame: the deepest marker path shared by all of its touchers."""
    for p in fg.passes:
        mp = p.marker_path
        for level in range(1, len(mp)):
            fg.frame_paths.add(mp[:level])
    pass_mp = dict((p.id, p.marker_path) for p in fg.passes)
    for node in fg.resources:
        touchers = set(node.writer_ids) | set(node.reader_ids)
        mps = [pass_mp[t] for t in touchers if t in pass_mp]
        if not mps:
            node.frame_path = ()
            continue
        common = mps[0]
        for mp in mps[1:]:
            limit = min(len(common), len(mp))
            i = 0
            while i < limit and common[i] == mp[i]:
                i += 1
            common = common[:i]
        while common and common not in fg.frame_paths:
            common = common[:-1]
        node.frame_path = tuple(common)


def _classify_internal(fg, folded_self_rw):
    """Mark resources that never leave their single toucher: a pure self-read-
    write of a private working set (the read may have been folded away, hence
    folded_self_rw). Write-only is NOT internal -- produced-but-unread content
    stays visible so the user can judge readback-vs-bug (this also exposes a
    present-less swapchain)."""
    for node in fg.resources:
        touchers = set(node.writer_ids) | set(node.reader_ids)
        self_rw = bool(node.reader_ids) or node.res_key in folded_self_rw
        node.internal = (bool(node.writer_ids) and self_rw and
                         len(touchers) == 1)


def build_graph(passes, usage_by_res, res_info, res_names=None, versioned=False,
                externally_written=None, boundaries=None):
    """Build the bipartite pass/resource graph from bucketed usage events.

    Merged mode (default): one node per resource; fg.rank_edges is a DAG-safe
    set for layout (drawn edges may flow backwards in time). versioned=True
    splits into version nodes on write-after-read so each read points at its
    producing write. Pass aggregation is the caller's (build_passes).

    externally_written: res_keys written OUTSIDE this view (e.g. elsewhere in
    the frame when scoped).
    usage_by_res: {res_key: [(eventId, usage_name), ...]} any order.
    res_info:     {res_key: {'kind': str, 'info': dict}}.
    boundaries:   {eid: 'begin'|'end'} for gap usage routing.
    """
    res_names = res_names or {}
    externally_written = externally_written or set()
    fg = FrameGraph()

    slots = _bucket_usages(passes, usage_by_res, boundaries)

    # Single writer, no other producer anywhere (here or externally_written):
    # the self-read is conservative UAV noise expressing no dependency, drop it.
    # Any other writer makes the read a real dependency.
    folded_self_rw = set()  # keys whose self-read was folded away
    for res_key, per_pass in slots.items():
        if res_key in externally_written:
            continue
        writers = [pi for pi in per_pass if per_pass[pi]['w']]
        if len(writers) != 1:
            continue
        slot = per_pass[writers[0]]
        if slot['r'] and slot['w']:
            slot['r'] = []
            folded_self_rw.add(res_key)

    fg.passes = list(passes)

    edge_index = {}

    def add_edge(src_id, dst_id, kind, usages):
        key = (src_id, dst_id, kind)
        e = edge_index.get(key)
        if e is None:
            e = Edge(src_id, dst_id, kind)
            edge_index[key] = e
            fg.edges.append(e)
        e.usages.extend(usages)

    version_count = {}

    def new_version(res_key, imported):
        version_count[res_key] = version_count.get(res_key, 0) + 1
        info = res_info.get(res_key, {})
        node = ResourceVersionNode(
            res_key,
            res_names.get(res_key, str(res_key)),
            version_count[res_key],
            info.get('kind', RES_COLOR),
            info.get('info'),
            imported=imported)
        fg.resources.append(node)
        return node

    latest = {}  # res_key -> ResourceVersionNode

    if versioned:
        for res_key in sorted(slots.keys()):
            per_pass = slots[res_key]
            cur = None
            for pi in sorted(per_pass.keys()):
                slot = per_pass[pi]
                p = fg.passes[pi]
                has_r = len(slot['r']) > 0
                has_w = len(slot['w']) > 0
                if has_r:
                    if cur is None:
                        # first touch is a read: no producing write inside this
                        # view, so it is imported (mirror of the merged branch's
                        # `imported = first_writer_pi is None`)
                        cur = new_version(res_key, imported=True)
                    add_edge(cur.id, p.id, READ, slot['r'])
                    cur.reader_ids.append(p.id)
                    if cur.first_read_eid is None:
                        cur.first_read_eid = slot['r'][0][0]
                if has_w:
                    if has_r or cur is None or cur.reader_ids:
                        cur = new_version(res_key, imported=False)
                    add_edge(p.id, cur.id, WRITE, slot['w'])
                    cur.writer_ids.append(p.id)
                    last_w = max(x[0] for x in slot['w'])
                    if cur.last_write_eid is None or last_w > cur.last_write_eid:
                        cur.last_write_eid = last_w
            if cur is not None:
                latest[res_key] = cur
    else:
        for res_key in sorted(slots.keys()):
            per_pass = slots[res_key]
            order_sorted = sorted(per_pass.keys())
            # output-first anchoring: resource ranks right of its first writer;
            # only later readers constrain ranking (earlier/self-RW reads draw
            # backwards but don't get a rank edge).
            first_writer_pi = None
            for pi in order_sorted:
                if per_pass[pi]['w']:
                    first_writer_pi = pi
                    break
            # imported == no producing write inside this view (mirror of the
            # versioned branch's first-touch-is-a-read case)
            node = new_version(res_key, imported=first_writer_pi is None)
            for pi in order_sorted:
                slot = per_pass[pi]
                p = fg.passes[pi]
                if slot['r']:
                    add_edge(node.id, p.id, READ, slot['r'])
                    node.reader_ids.append(p.id)
                    if node.first_read_eid is None:
                        node.first_read_eid = slot['r'][0][0]
                    if first_writer_pi is None or pi > first_writer_pi:
                        fg.rank_edges.append(Edge(node.id, p.id, EDGE_RANK))
                if slot['w']:
                    add_edge(p.id, node.id, WRITE, slot['w'])
                    node.writer_ids.append(p.id)
                    last_w = max(x[0] for x in slot['w'])
                    if node.last_write_eid is None or last_w > node.last_write_eid:
                        node.last_write_eid = last_w
            if first_writer_pi is not None:
                fg.rank_edges.append(
                    Edge(fg.passes[first_writer_pi].id, node.id, EDGE_RANK))
            for a, b in zip(order_sorted, order_sorted[1:]):
                fg.rank_edges.append(
                    Edge(fg.passes[a].id, fg.passes[b].id, EDGE_RANK))
            latest[res_key] = node

    if versioned:
        fg.rank_edges = list(fg.edges)  # versioned construction is already a DAG

    _stamp_version_counts(fg, version_count)
    _detect_orphans(fg)
    _assign_frame_paths(fg)
    _classify_internal(fg, folded_self_rw)
    return fg


# ----------------------------------------------------------------- runtime
# Everything below touches renderdoc (imported lazily) and runs on the
# replay thread only.

def _collect_leaves(rd, roots, sdfile, key_of, present_resolver=None):
    """Flatten the action tree into LeafAction IR. Recurse into PushMarker
    regions and grouping nodes; a MultiAction with its own draw/dispatch kind
    stays a single leaf, while a kind-less MultiAction container is recursed
    so its child executable is kept. Only semantic
    debug markers contribute to marker_path - API-structure groupings
    (vkQueueSubmit, render-pass regions, command buffers) are recursed but
    excluded so pass names stay meaningful.

    Returns (leaves, boundaries).
    """
    f_draw = action_flags.draw(rd)
    f_dispatch = action_flags.dispatch(rd)
    f_clear = action_flags.flag(rd, 'Clear')
    f_transfer = action_flags.transfer(rd)
    f_present = action_flags.flag(rd, 'Present')
    f_push = action_flags.flag(rd, 'PushMarker')
    f_multi = action_flags.flag(rd, 'MultiAction')
    f_beginpass = action_flags.flag(rd, 'BeginPass')
    f_endpass = action_flags.flag(rd, 'EndPass')
    f_structural = action_flags.structural(rd)

    leaves = []
    boundaries = {}

    def action_name(act):
        if act.customName:
            return act.customName
        try:
            return act.GetName(sdfile)
        except Exception:
            return ''

    def is_semantic_marker(f, name):
        if f & f_structural:
            return False
        if not name:
            return False
        if _API_CALL_RE.match(name):
            return False
        for prefix in _STRUCTURAL_NAME_PREFIXES:
            if name.startswith(prefix):
                return False
        return True

    def visit(act, path):
        f = act.flags
        if f & f_endpass:
            boundaries[act.eventId] = 'end'
        elif f & f_beginpass:
            boundaries[act.eventId] = 'begin'
        kind = None
        if f & f_draw:
            kind = KIND_DRAW
        elif f & f_dispatch:
            kind = KIND_DISPATCH
        elif f & f_clear:
            kind = KIND_CLEAR
        elif f & f_transfer:
            kind = KIND_TRANSFER
        elif f & f_present:
            kind = KIND_PRESENT

        children = list(act.children)
        # Recurse into marker, grouping, and kind-less container nodes.
        # MultiAction nodes with their own draw/dispatch kind stay as leaves.
        if (children and (kind is None or (f & f_push)) and
                not (f & f_multi and kind is not None)):
            newpath = path
            if f & f_push:
                nm = action_name(act)
                if nm and is_semantic_marker(f, nm):
                    newpath = path + (nm,)
            for c in children:
                visit(c, newpath)
            return
        if kind is None:
            return
        outs = []
        for o in act.outputs:
            k = key_of(o)
            if k is not None:
                outs.append(k)
        copy_src = key_of(act.copySource)
        if kind == KIND_PRESENT and copy_src is None:
            # Some APIs expose the presented backbuffer as copyDestination.
            copy_src = key_of(act.copyDestination)
        if (kind == KIND_PRESENT and copy_src is None and
                present_resolver is not None):
            try:
                copy_src = present_resolver.resolve(act)
            except Exception:
                copy_src = None
        leaves.append(LeafAction(
            act.eventId, kind,
            group_outputs=outs,
            group_depth=key_of(act.depthOut),
            marker_path=path,
            name=action_name(act),
            copy_src_hint=copy_src,
            copy_dst_hint=key_of(act.copyDestination)))

    for a in roots:
        visit(a, ())
    return leaves, boundaries


def texture_kind_of(cf, texcat, cands=None):
    """Candidate gate + node-kind for a texture's creationFlags; None when the
    config excludes it. texcat=rd.TextureCategory (param'd for testing).

    Classification and admission use the same priority order. A texture that
    is classified as swapchain is controlled only by the swapchain switch, even
    if RenderDoc also marks it as a color target."""
    if cands is None:
        cands = _config.candidates_of(_config.DEFAULTS)
    if cf & texcat.SwapBuffer:
        return RES_SWAPCHAIN if cands.get(_config.KEY_TEX_SWAP) else None
    if cf & texcat.DepthTarget:
        return RES_DEPTH if cands.get(_config.KEY_TEX_DEPTH) else None
    if cf & texcat.ColorTarget:
        return RES_COLOR if cands.get(_config.KEY_TEX_COLOR) else None
    if cf & texcat.ShaderReadWrite:
        return RES_UAV_TEX if cands.get(_config.KEY_TEX_RW) else None
    return RES_SAMPLED if cands.get(_config.KEY_TEX_OTHER) else None


def buffer_admitted(cf, bufcat, cands=None):
    """Candidate gate for a buffer's creationFlags. bufcat=rd.BufferCategory.
    'buf_noflags' admits creationFlags==0 buffers (copy dst / readback staging,
    invisible to every category mask)."""
    if cands is None:
        cands = _config.candidates_of(_config.DEFAULTS)
    if cands.get(_config.KEY_BUF_NOFLAGS) and not cf:
        return True
    mask = 0
    if cands.get(_config.KEY_BUF_RW):
        mask |= bufcat.ReadWrite
    if cands.get(_config.KEY_BUF_INDIRECT):
        mask |= bufcat.Indirect
    if cands.get(_config.KEY_BUF_VERTEX_INDEX):
        mask |= bufcat.Vertex | bufcat.Index
    if cands.get(_config.KEY_BUF_CONSTANTS):
        mask |= bufcat.Constants
    return bool(cf & mask)


def _make_present_resolver(controller, chunks):
    if not chunks:
        return None
    try:
        resolver_cls = apis.present_resolver(apis.api_key(controller))
        if resolver_cls is None:
            return None
        return resolver_cls(chunks)
    except Exception:
        return None


def _add_present_usages(usage_by_res, leaves, res_info):
    """RenderDoc does not report Present as ResourceUsage. Convert the extracted
    Present source hint into a normal read usage so graph building, scoped views
    and portals all use the same path."""
    for leaf in leaves:
        if leaf.kind != KIND_PRESENT or leaf.copy_src_hint is None:
            continue
        res_key = leaf.copy_src_hint
        if res_key not in res_info:
            continue
        usage_by_res.setdefault(res_key, []).append((leaf.eid, 'Present'))


def _apply_usage_cleanup(usage_by_res, result):
    usage_cleanup.apply_label_cleanup(usage_by_res, result)


def _apply_depth_refinement(usage_by_res, result):
    result = result or {}
    usage_access.apply_depth_access(usage_by_res, result.get('access') or {})


def _apply_shader_refinement(usage_by_res, result):
    usage_access.apply_shader_access(usage_by_res, result or {})


def _usage_refinements(controller, rd, usage_by_res, leaves,
                       refinement_cache, parse_shaders, progress, warnings):
    """Return [(bundle_key, result, apply_fn), ...] for usage refinement."""
    refinement_cache = refinement_cache or {}
    cleanup_result = usage_cleanup.collect_label_cleanup(controller, rd)
    depth_result = depth_access.refine(
        controller, rd, usage_by_res, leaves,
        cached=refinement_cache.get('depth_access'),
        progress=progress, warnings=warnings)
    shader_result = (shader_refinement.refine(controller, rd, warnings=warnings)
                     if parse_shaders else {})
    return [
        ('usage_cleanup', cleanup_result, _apply_usage_cleanup),
        ('depth_access', depth_result, _apply_depth_refinement),
        ('shader_access', shader_result, _apply_shader_refinement),
    ]


def _apply_usage_refinements(usage_by_res, refinements):
    for _key, result, apply_fn in refinements:
        apply_fn(usage_by_res, result)


def _refinement_bundle_values(refinements):
    return dict((key, result) for key, result, _apply_fn in refinements)


def extract_bundle(controller, include_buffers=True, candidates=None,
                   refinement_cache=None, progress=None, parse_shaders=False):
    """Pull actions + per-resource usage into a plain-Python bundle. Replay
    thread only. The graph is built from the bundle on the UI side so
    marker-depth/versioning toggles re-render without another replay.

    candidates: config.candidates_of()-shaped dict gating which resources enter
    (None = defaults). include_buffers gates the buffer pass as a whole.
    refinement_cache: cached refinement payloads from a prior extraction of
    the SAME capture (eids are stable). progress(done, total) reports the
    depth walk when it runs.
    parse_shaders: run the same extraction-stage static shader-access pass and
    bake its read/write de-edging into usage_by_res."""
    import renderdoc as rd

    t0 = time.time()
    warnings = []
    null_rid = rd.ResourceId.Null()

    def key_of(rid):
        if rid is None or rid == null_rid:
            return None
        return str(rid)

    res_names = {}
    for res in controller.GetResources():
        k = key_of(res.resourceId)
        if k is not None:
            res_names[k] = res.name

    res_info = {}
    rid_objects = {}

    if candidates is None:
        candidates = _config.candidates_of(_config.DEFAULTS)

    for tex in controller.GetTextures():
        kind = texture_kind_of(tex.creationFlags, rd.TextureCategory,
                               candidates)
        if kind is None:
            continue
        k = key_of(tex.resourceId)
        if k is None:
            continue
        try:
            fmt = tex.format.Name()
        except Exception:
            fmt = ''
        res_info[k] = {'kind': kind, 'info': {
            'dims': '%dx%d' % (tex.width, tex.height),
            'format': fmt,
            'msaa': int(getattr(tex, 'msSamp', 1)),
        }}
        rid_objects[k] = tex.resourceId

    if include_buffers:
        for buf in controller.GetBuffers():
            if not buffer_admitted(buf.creationFlags, rd.BufferCategory,
                                   candidates):
                continue
            k = key_of(buf.resourceId)
            if k is None or k in res_info:
                continue
            res_info[k] = {'kind': RES_BUFFER, 'info': {
                'dims': '%d bytes' % buf.length, 'format': 'buffer', 'msaa': 1,
            }}
            rid_objects[k] = buf.resourceId

    usage_name_of = {}
    for uname in USAGE_ACCESS:
        val = getattr(rd.ResourceUsage, uname, None)
        if val is not None:
            usage_name_of[val] = uname

    usage_by_res = {}
    for k in res_info:
        try:
            evs = controller.GetUsage(rid_objects[k])
        except Exception as exc:
            warnings.append('GetUsage failed for %s: %s' % (res_names.get(k, k), exc))
            continue
        lst = []
        for ev in evs:
            uname = usage_name_of.get(ev.usage)
            if uname is not None:
                lst.append((ev.eventId, uname))
        if lst:
            usage_by_res[k] = lst

    sdfile = controller.GetStructuredFile()
    chunks = getattr(sdfile, 'chunks', ())
    present_resolver = _make_present_resolver(controller, chunks)
    leaves, pass_boundaries = _collect_leaves(
        rd, controller.GetRootActions(), sdfile, key_of,
        present_resolver=present_resolver)
    _add_present_usages(usage_by_res, leaves, res_info)

    refinements = _usage_refinements(
        controller, rd, usage_by_res, leaves, refinement_cache, parse_shaders,
        progress, warnings)
    _apply_usage_refinements(usage_by_res, refinements)
    refinement_values = _refinement_bundle_values(refinements)

    return {
        'leaves': leaves,
        'usage_by_res': usage_by_res,
        'pass_boundaries': pass_boundaries,
        'res_info': res_info,
        'res_names': res_names,
        'rid_objects': rid_objects,
        'refinements': refinement_values,
        'refinement_cache': {
            'depth_access': refinement_values['depth_access'],
        },
        'warnings': warnings,
        'seconds': time.time() - t0,
    }


LARGE_FRAME_PASS_WARN = 2000   # pass-node count above which the UI warns


def _finalize_from_bundle(fg, bundle):
    """Attach bundle-level metadata to a freshly built graph: warnings,
    resource-id objects, unused-binding flags from shader-access refinement,
    and the summary stats."""
    fg.warnings = list(bundle['warnings']) + fg.warnings
    fg.rid_objects = bundle['rid_objects']
    refinements = bundle.get('refinements') or {}
    usage_access.apply_unused_binding_flags(
        fg, refinements.get('shader_access') or {})
    fg.stats = {
        'passes': len(fg.passes),
        'resources': len(fg.resources),
        'edges': len(fg.edges),
        'seconds': bundle['seconds'],
    }
    return fg


def build_from_bundle(bundle, marker_depth=None, versioned=False,
                      collapsed=None):
    """Build the FrameGraph from a bundle. Pure Python, any thread."""
    passes = build_passes(bundle['leaves'], bundle['res_names'], marker_depth,
                          collapsed=collapsed)
    fg = build_graph(passes, bundle['usage_by_res'], bundle['res_info'],
                     bundle['res_names'], versioned=versioned,
                     boundaries=bundle.get('pass_boundaries'))
    _finalize_from_bundle(fg, bundle)
    if len(fg.passes) > LARGE_FRAME_PASS_WARN:
        fg.warnings.append('Very large frame (%d pass nodes); consider '
                           'disabling buffers or using the filter.' % len(fg.passes))
    return fg


# portal sort key floor: every real pass orders below this, so portals trail
# them. graph_layout densifies order values for cyclic layouts and relies on
# this same base.
PORTAL_ORDER_BASE = 10 ** 6


class PortalNode(object):
    """Stand-in for an EXTERNAL scope instance touching a focus-view resource.
    Duck-types PassNode for layout/rendering; double-click jumps to
    portal_path/portal_range."""

    def __init__(self, idx, path, rng, role, name=None, focus_eid=None):
        self.node_type = NODE_PORTAL
        self.id = 'portal%d' % idx
        self.order = PORTAL_ORDER_BASE + idx  # sorts after every real pass
        self.kind = CAT_PORTAL
        self.name = name if name is not None else path[-1]
        self.marker_path = tuple(path)
        self.portal_path = tuple(path)
        self.portal_range = (int(rng[0]), int(rng[1]))
        self.first_eid, self.last_eid = self.portal_range
        # producer portals emit writes, consumer receive reads: dual-role
        # scopes are SPLIT so portal edges can't close a rank cycle
        self.portal_role = role
        # set when the portal stands in for a single NODE (not a scope); the
        # jump focuses this eid in the ancestor's one-level view
        self.portal_focus_eid = focus_eid
        self.action_count = 0
        self.leaves = []
        self.drillable = False
        self.collapsed_frame = False
        self.bundle_members = None
        self.bundle_member_eids = []

    @property
    def frame_path(self):
        return self.marker_path[:-1]


def _leaf_runs(leaves, prefix):
    """Contiguous instance ranges of a marker prefix in leaf order."""
    k = len(prefix)
    prefix = tuple(prefix)
    runs = []
    cur = None
    for leaf in leaves:
        if tuple(leaf.marker_path[:k]) == prefix:
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


def _run_containing(runs, eid):
    for a, b in runs:
        if a <= eid <= b:
            return (a, b)
    if not runs:
        return None
    return min(runs, key=lambda r: min(abs(r[0] - eid), abs(r[1] - eid)))


def scope_chain(bundle, path, eid):
    """Breadcrumb ancestry: one entry per level of `path`, each the instance
    range containing eid."""
    chain = []
    for k in range(1, len(path) + 1):
        prefix = tuple(path[:k])
        rng = _run_containing(_leaf_runs(bundle['leaves'], prefix), eid)
        if rng is None:
            return []
        chain.append({'label': prefix[-1], 'path': prefix, 'range': rng})
    return chain


def _attach_scope_portals(fg, bundle, scope_path, scope_range, level,
                          outside_events, versioned, bundling):
    """Append external-scope portal nodes + edges to fg in place. Each outside
    event maps to the scope instance one level below its common prefix with the
    current scope; producers feed the imported head episode, the tail feeds
    consumers, and double-click jumps there. Called only when scope_range is not
    None and outside_events is non-empty (the caller gates that)."""
    all_leaves = bundle['leaves']
    leaf_eids = [l.eid for l in all_leaves]

    def leaf_at(eid):
        i = bisect.bisect_left(leaf_eids, eid)
        cands = []
        if i < len(all_leaves):
            cands.append(all_leaves[i])
        if i > 0:
            cands.append(all_leaves[i - 1])
        if not cands:
            return None
        return min(cands, key=lambda l: abs(l.eid - eid))

    portals = {}
    portal_list = []
    run_cache = {}
    view_cache = {}

    def make_portal(key, path, rng, role, name=None, focus_eid=None):
        portal = portals.get(key)
        if portal is None:
            portal = PortalNode(len(portal_list) + 1, path, rng, role,
                                name=name, focus_eid=focus_eid)
            portals[key] = portal
            portal_list.append(portal)
        return portal

    def ancestor_node_portal(eid, anchor, role):
        # Toucher hangs directly under the common ancestor (no deeper
        # level), so an ancestor-scope portal carries no info; stand in for
        # the NODE instead, resolved through the SAME parse pipeline
        # (including bundling) the jump uses, so the portal IS the node
        # landed on.
        if anchor not in run_cache:
            run_cache[anchor] = _leaf_runs(all_leaves, anchor)
        rng = _run_containing(run_cache[anchor], eid)
        if rng is None:
            return None
        vkey = (anchor, rng)
        view = view_cache.get(vkey)
        if view is None:
            view = build_scoped(bundle, anchor, rng,
                                versioned=versioned,
                                bundling=bundling,
                                make_portals=False).passes
            view_cache[vkey] = view
        target = None
        for p in view:
            if p.first_eid <= eid <= p.last_eid:
                target = p
                break
        if target is None:
            return None
        key = (anchor, rng, target.first_eid, target.last_eid, role)
        portal = make_portal(key, anchor, rng, role,
                             name=target.name,
                             focus_eid=target.first_eid)
        members = getattr(target, 'bundle_members', None)
        if members and not getattr(portal, 'bundle_members', None):
            portal.bundle_members = list(members)
            portal.bundle_member_eids = list(
                getattr(target, 'bundle_member_eids', None) or [])
        return portal

    def portal_for(eid, role):
        leaf = leaf_at(eid)
        if leaf is None:
            return None
        mpath = tuple(leaf.marker_path)
        common = 0
        while (common < len(mpath) and common < level and
               mpath[common] == scope_path[common]):
            common += 1
        plevel = common + 1 if common < level else level
        if plevel > len(mpath):
            # never resolve to an ancestor of the current scope
            return ancestor_node_portal(eid, mpath[:common], role)
        ppath = mpath[:plevel]
        if ppath not in run_cache:
            run_cache[ppath] = _leaf_runs(all_leaves, ppath)
        rng = _run_containing(run_cache[ppath], eid)
        if rng is None:
            return None
        if ppath == scope_path and rng == tuple(scope_range):
            return None  # ourselves (boundary event), not a portal
        return make_portal((ppath, rng, role), ppath, rng, role)

    first_ep = {}
    last_ep = {}
    written_inside = set()
    for node in fg.resources:
        # bundled nodes answer for every member key so raw-id outside events find them
        keys = (getattr(node, 'bundle_member_keys', None) or
                [node.res_key])
        for k in keys:
            first_ep.setdefault(k, node)
            last_ep[k] = node
        if node.writer_ids:
            written_inside.update(keys)
    edge_index = dict(((e.src_id, e.dst_id, e.kind), e)
                      for e in fg.edges)

    def add_portal_edge(src_id, dst_id, kind, eid, uname):
        key = (src_id, dst_id, kind)
        e = edge_index.get(key)
        if e is None:
            e = Edge(src_id, dst_id, kind)
            edge_index[key] = e
            fg.edges.append(e)
            fg.rank_edges.append(Edge(src_id, dst_id, EDGE_RANK))
        e.usages.append((eid, uname))

    for res_key in sorted(outside_events.keys()):
        head = first_ep.get(res_key)
        tail = last_ep.get(res_key)
        if head is None:
            continue  # resource has no presence inside the scope
        for eid, uname, acc in outside_events[res_key]:
            # producer edge for eid < scope_start
            if (acc in (WRITE, RW) and head.imported
                    and eid < scope_range[0]):
                portal = portal_for(eid, ROLE_PRODUCER)
                if portal is not None:
                    add_portal_edge(portal.id, head.id, WRITE,
                                    eid, uname)
            # tail -> consumer only from reads AFTER the scope (an earlier
            # read consumed a prior version)
            if acc in (READ, RW) and eid > scope_range[1]:
                # only for resources THIS scope wrote: external readers of a
                # pure input are siblings with no causal link
                if res_key not in written_inside:
                    continue
                portal = portal_for(eid, ROLE_CONSUMER)
                if portal is not None:
                    add_portal_edge(tail.id, portal.id, READ,
                                    eid, uname)
    fg.passes = list(fg.passes) + portal_list


def _partition_scope_usage(usage, scope_range):
    """Split each resource's events into those inside the scope's eid range and
    a (reads, writes) tally of outside activity. Returns (scoped_usage, outside,
    outside_events), the last keeping the outside (eid, uname, access) triples
    for portal routing."""
    scoped_usage = {}
    outside = {}
    outside_events = {}
    for res_key, evs in usage.items():
        inside_evs = []
        out_r = 0
        out_w = 0
        for eid, uname in evs:
            acc = usage_access.direction(uname)
            if scope_range is None or scope_range[0] <= eid <= scope_range[1]:
                inside_evs.append((eid, uname))
            elif acc != IGNORE:
                if acc in (READ, RW):
                    out_r += 1
                if acc in (WRITE, RW):
                    out_w += 1
                outside_events.setdefault(res_key, []).append(
                    (eid, uname, acc))
        if inside_evs:
            scoped_usage[res_key] = inside_evs
        outside[res_key] = (out_r, out_w)
    return scoped_usage, outside, outside_events


def _annotate_outside_activity(fg, outside, outside_events):
    """Stamp each resource with its out-of-scope reader/writer counts and the
    identity (eid set) of its external writers."""
    ow_eids = {}
    for res_key, evs in outside_events.items():
        s = set(eid for (eid, _u, acc) in evs if acc in (WRITE, RW))
        if s:
            ow_eids[res_key] = frozenset(s)
    for node in fg.resources:
        out_r, out_w = outside.get(node.res_key, (0, 0))
        node.outside_readers = out_r
        node.outside_writers = out_w
        node.outside_write_eids = ow_eids.get(node.res_key, frozenset())
        node.scope_input = bool(node.imported and out_w > 0)
        if out_r > 0:
            node.internal = False  # consumed elsewhere in the frame


def build_scoped(bundle, scope_path, scope_range, versioned=True,
                 bundling=False, make_portals=True):
    """Focus view: build ONE level of children inside a scope. A scope is a
    marker INSTANCE (absolute path + the contiguous eid range of that
    occurrence; same-named markers elsewhere are different instances).
    scope_path=() / scope_range=None is the whole-frame root.

    Children are consecutive runs one level deeper (drillable when they hold
    deeper structure or >1 action); leaves at the scope path use fine rules.
    Each resource carries outside_readers/writers from the rest of the frame;
    scope_input marks written-outside-read-inside inputs."""
    scope_path = tuple(scope_path)
    level = len(scope_path)

    leaves = []
    for act in bundle['leaves']:
        if scope_range is not None and not (
                scope_range[0] <= act.eid <= scope_range[1]):
            continue
        if tuple(act.marker_path[:level]) != scope_path:
            continue
        leaves.append(act)

    passes = build_passes(leaves, bundle['res_names'], scope_level=level)
    for p in passes:
        if p.kind == CAT_PRESENT or len(p.marker_path) <= level:
            continue  # fine groups / present never drillable
        deeper = any(len(l.marker_path) > level + 1 for l in p.leaves)
        p.drillable = deeper or len(p.leaves) >= 2

    scoped_usage, outside, outside_events = _partition_scope_usage(
        bundle['usage_by_res'], scope_range)

    fg = build_graph(passes, scoped_usage, bundle['res_info'],
                     bundle['res_names'], versioned=versioned,
                     externally_written=set(
                         k for k, (_r, w) in outside.items() if w > 0),
                     boundaries=bundle.get('pass_boundaries'))
    _annotate_outside_activity(fg, outside, outside_events)

    # ---- pre-portal bundling
    if bundling:
        bundle_equivalent_resources(fg)
        bundle_equivalent_passes(fg)

    # ---- portals for external scopes: each outside event maps to the instance
    # one level below the common prefix with this scope; producers feed the
    # imported head episode, the tail feeds consumers. Double-click jumps there.
    if make_portals and scope_range is not None and outside_events:
        _attach_scope_portals(fg, bundle, scope_path, scope_range, level,
                              outside_events, versioned, bundling)

    _finalize_from_bundle(fg, bundle)
    return fg


BUNDLE_MIN_MEMBERS = 3

_NAME_TOKEN_RE = re.compile(r'\d+|[A-Z]{2,}(?![a-z])|[A-Z][a-z]*|[a-z]+')


def _name_signature(name):
    """Name-similarity signature: split on separators/camelCase/digit
    boundaries, lowercase, digit runs -> '#'. Returns (first, last) token so
    XXX_Mip0/XXX_Mip1 and Mesh_X_Buffer/Mesh_Y_Buffer count as similar."""
    tokens = []
    for part in re.split(r'[^0-9A-Za-z]+', name):
        if not part:
            continue
        for tok in _NAME_TOKEN_RE.findall(part):
            tokens.append('#' if tok.isdigit() else tok.lower())
    if not tokens:
        return (name.lower(),)
    if len(tokens) == 1:
        return (tokens[0],)
    return (tokens[0], tokens[-1])


def _aggregate_member_fields(target, members):
    """Fold the cross-frame activity a bundle / collapsed-generation head
    inherits from its members onto target. Shared by resource bundling and
    generation collapse; the writer/reader id sets differ between those two and
    stay at the call sites."""
    target.scope_input = any(getattr(m, 'scope_input', False) for m in members)
    target.outside_readers = sum(getattr(m, 'outside_readers', 0)
                                 for m in members)
    target.outside_writers = sum(getattr(m, 'outside_writers', 0)
                                 for m in members)
    target.internal = all(getattr(m, 'internal', False) for m in members)
    last_writes = [getattr(m, 'last_write_eid', None) for m in members]
    last_writes = [e for e in last_writes if e is not None]
    target.last_write_eid = max(last_writes) if last_writes else None


def bundle_equivalent_resources(fg, min_members=BUNDLE_MIN_MEMBERS):
    """Bundle resources into one member-listing node when BOTH hold: (1)
    identical edge structure (same kind, writer set, reader set); (2) similar
    names (_name_signature). Groups smaller than min_members stay separate.
    bundle_members/bundle_member_keys are parallel lists for display and
    navigation; edges/rank edges are remapped and deduped in place."""
    def group_key(node):
        # internal is in the signature: a folded self-RW working set and a
        # write-only resource can share edge structure, but bundling them would
        # hide the one deserving scrutiny.
        # outside_write_eids: scope inputs from different external events are
        # different behaviours though the in-view writer set is empty; pure
        # external inputs all carry the empty set and keep bundling.
        return (node.res_kind, frozenset(node.writer_ids),
                frozenset(node.reader_ids), _name_signature(node.name),
                bool(getattr(node, 'internal', False)),
                getattr(node, 'outside_write_eids', frozenset()))

    groups = {}
    for node in fg.resources:
        groups.setdefault(group_key(node), []).append(node)

    remap = {}
    new_resources = []
    emitted = set()
    bundle_idx = 0
    for node in fg.resources:
        key = group_key(node)
        members = groups[key]
        if len(members) < min_members:
            new_resources.append(node)
            continue
        if key in emitted:
            continue  # remap entries were created with the bundle
        emitted.add(key)
        bundle_idx += 1
        by_name = {}
        for m in members:
            by_name.setdefault(m.name, m.res_key)
        names = sorted(by_name.keys())
        prefix = names[0]
        for nm in names[1:]:
            i = 0
            while i < min(len(prefix), len(nm)) and prefix[i] == nm[i]:
                i += 1
            prefix = prefix[:i]
        if len(prefix) >= 3:
            label = u'%s… ×%d' % (prefix, len(members))
        else:
            label = tr('×%d resources') % len(members)
        # res_key derived from the member set (not a running index) so every
        # write generation of one family shares it, letting episode badges and
        # generation collapse work across bundles.
        family_key = 'bundle:' + '|'.join(sorted(by_name.values()))
        bundle = ResourceVersionNode(
            family_key, label, node.version, node.res_kind,
            {'dims': tr('%d resources') % len(members)},
            imported=node.imported)
        bundle.id = 'bundle%d' % bundle_idx
        bundle.writer_ids = list(node.writer_ids)
        bundle.reader_ids = list(node.reader_ids)
        bundle.frame_path = node.frame_path
        _aggregate_member_fields(bundle, members)
        bundle.bundle_members = names
        bundle.bundle_member_keys = [by_name[n] for n in names]
        for m in members:
            remap[m.id] = bundle.id
        new_resources.append(bundle)

    if not remap:
        return fg
    fg.resources = new_resources
    _apply_node_remap(fg, remap)
    _collapse_bundle_generations(fg)
    return fg


# at this many write generations the per-generation nodes repeat explosively;
# below it episode twins keep #k badges exact, at/above compactness wins.
GENERATION_COLLAPSE_THRESHOLD = 4


def _collapse_bundle_generations(fg, threshold=GENERATION_COLLAPSE_THRESHOLD):
    """Merge same-family bundle generations (identical member-key sets) into one
    node at the threshold. All generations' writers/readers attach to it -
    locally merged semantics (the accepted price for not repeating a huge
    member list N times)."""
    families = {}
    for node in fg.resources:
        keys = getattr(node, 'bundle_member_keys', None)
        if keys:
            families.setdefault(
                (node.res_kind, frozenset(keys)), []).append(node)

    remap = {}
    drop = set()
    for gens in families.values():
        if len(gens) < threshold:
            continue
        gens.sort(key=lambda n: n.version)
        head = gens[0]
        head.generations = len(gens)
        head.version = 1  # single node again: no episode badge
        head.name = tr('%s (%d generations)') % (head.name, len(gens))
        seen_w = set()
        seen_r = set()
        head.writer_ids = [i for g in gens for i in g.writer_ids
                           if not (i in seen_w or seen_w.add(i))]
        head.reader_ids = [i for g in gens for i in g.reader_ids
                           if not (i in seen_r or seen_r.add(i))]
        _aggregate_member_fields(head, gens)
        for g in gens[1:]:
            remap[g.id] = head.id
            drop.add(g.id)

    if not remap:
        return fg
    fg.resources = [n for n in fg.resources if n.id not in drop]
    _apply_node_remap(fg, remap)
    return fg


def _remap_edges(edge_list, remap):
    """Re-point edges through a node-id remap, merging duplicates; usages
    kept in event order."""
    out = []
    index = {}
    for e in edge_list:
        src = remap.get(e.src_id, e.src_id)
        dst = remap.get(e.dst_id, e.dst_id)
        k = (src, dst, e.kind)
        existing = index.get(k)
        if existing is None:
            ne = Edge(src, dst, e.kind)
            ne.usages = list(e.usages)
            index[k] = ne
            out.append(ne)
        else:
            existing.usages.extend(e.usages)
    for e in out:
        e.usages.sort(key=lambda t: t[0])
    return out


def _apply_node_remap(fg, remap, remap_orphans=False):
    """Re-point every edge (and optionally orphan-pass ids) through a node-id
    remap, in place. Only pass-bundling remaps pass ids, so remap_orphans is
    opt-in (resource bundling and generation collapse touch no pass ids)."""
    fg.edges = _remap_edges(fg.edges, remap)
    fg.rank_edges = _remap_edges(fg.rank_edges, remap)
    if remap_orphans and fg.orphan_pass_ids:
        fg.orphan_pass_ids = set(remap.get(i, i) for i in fg.orphan_pass_ids)


_PASS_SUFFIX_RE = re.compile(r'\s+#\d+$')


def bundle_equivalent_passes(fg, min_members=BUNDLE_MIN_MEMBERS):
    """Bundle equivalent leaf-level passes by edge structure and name."""
    reads = {}
    writes = {}
    for e in fg.edges:
        if e.kind == READ:
            reads.setdefault(e.dst_id, set()).add(e.src_id)
        elif e.kind == WRITE:
            writes.setdefault(e.src_id, set()).add(e.dst_id)

    def eligible(p):
        return (getattr(p, 'kind', '') not in (CAT_PORTAL, CAT_PRESENT) and
                not getattr(p, 'drillable', False) and
                not getattr(p, 'bundle_members', None))

    def group_key(p):
        base = _PASS_SUFFIX_RE.sub('', p.name)
        return (p.kind, _name_signature(base),
                frozenset(reads.get(p.id, ())),
                frozenset(writes.get(p.id, ())))

    groups = {}
    for p in fg.passes:
        if eligible(p):
            groups.setdefault(group_key(p), []).append(p)

    # split each group into consecutive execution runs
    pos = {}
    for i, p in enumerate(sorted(fg.passes, key=lambda q: q.order)):
        pos[p.id] = i

    seg_head = {}   # member id -> (head, members tuple)
    for members in groups.values():
        members.sort(key=lambda m: pos[m.id])
        seg = [members[0]]
        segs = [seg]
        for prev, cur in zip(members, members[1:]):
            if pos[cur.id] == pos[prev.id] + 1:
                seg.append(cur)
            else:
                seg = [cur]
                segs.append(seg)
        for s in segs:
            if len(s) >= min_members:
                for m in s:
                    seg_head[m.id] = (s[0], tuple(s))

    remap = {}
    new_passes = []
    for p in fg.passes:
        ent = seg_head.get(p.id)
        if ent is None:
            new_passes.append(p)
            continue
        head, members = ent
        if p is not head:
            remap[p.id] = head.id
            continue
        names = [m.name for m in members]
        head.bundle_members = names
        head.bundle_member_eids = [m.first_eid for m in members]
        head.name = u'%s ×%d' % (_PASS_SUFFIX_RE.sub('', names[0]),
                                 len(members))
        head.first_eid = min(m.first_eid for m in members)
        head.last_eid = max(m.last_eid for m in members)
        head.action_count = sum(m.action_count for m in members)
        head.leaves = [leaf for m in members for leaf in m.leaves]
        new_passes.append(head)

    if not remap:
        return fg
    fg.passes = new_passes
    _apply_node_remap(fg, remap, remap_orphans=True)
    return fg
