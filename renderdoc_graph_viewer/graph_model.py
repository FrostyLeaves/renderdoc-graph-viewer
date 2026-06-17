# -*- coding: utf-8 -*-
"""Frame-graph extraction. Everything below the `runtime` divider touches the
renderdoc module (imported lazily, replay thread); the rest is pure Python.
"""

import bisect
import re
import time

from .i18n import tr

from . import config as _config
# shared structured-data accessors; _child_int is the same defensive int reader
# descriptor_access uses (_ci)
from ._sdutil import _ci as _child_int, _rid_str

# Names matching this are raw-API-call groupings (vkCmd...(...)), not semantic markers.
_API_CALL_RE = re.compile(r'^(vk|gl|wgl|egl)[A-Za-z0-9_]*\(')
_STRUCTURAL_NAME_PREFIXES = (
    'ExecuteCommandList', 'Command Buffer', 'CommandBuffer', 'API Calls',
)

READ = 'read'
WRITE = 'write'
RW = 'rw'
IGNORE = 'ignore'
ACCESS_NONE = 'none'   # depth-access result: no attachment bound (distinct from IGNORE)
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

_STAGES = ('VS', 'HS', 'DS', 'GS', 'PS', 'CS', 'TS', 'MS', 'All')

# ResourceUsage enum-name -> READ/WRITE/RW/IGNORE access category.
USAGE_ACCESS = {}
for _s in _STAGES:
    USAGE_ACCESS['%s_Constants' % _s] = READ
    USAGE_ACCESS['%s_Resource' % _s] = READ
    USAGE_ACCESS['%s_RWResource' % _s] = RW
    USAGE_ACCESS['%s_ReadResource' % _s] = READ    # shader-refined: RW used read-only
    USAGE_ACCESS['%s_WriteResource' % _s] = WRITE  # shader-refined: RW used write-only
USAGE_ACCESS.update({
    'VertexBuffer': READ,
    'IndexBuffer': READ,
    'InputTarget': READ,
    'Indirect': READ,
    'CopySrc': READ,
    'ResolveSrc': READ,
    'ColorTarget': WRITE,
    'DepthStencilTarget': WRITE,
    # RenderDoc reports every depth binding as write-class DepthStencilTarget;
    # _refine_depth_access rewrites test-only bindings to these per-draw.
    'DepthTestRead': READ,
    'DepthTestRW': RW,
    'Clear': WRITE,
    'Discard': WRITE,
    'CopyDst': WRITE,
    'ResolveDst': WRITE,
    'StreamOut': WRITE,
    'Copy': RW,
    'Resolve': RW,
    'GenMips': RW,
    'Barrier': IGNORE,
    'Unused': IGNORE,
    'CPUWrite': IGNORE,
})


class LeafAction(object):
    """One executable action flattened out of the action tree (IR)."""

    __slots__ = ('eid', 'kind', 'outputs', 'depth_out', 'marker_path',
                 'name', 'copy_src', 'copy_dst')

    def __init__(self, eid, kind, outputs=(), depth_out=None, marker_path=(),
                 name='', copy_src=None, copy_dst=None):
        self.eid = eid
        self.kind = kind  # 'draw' | 'dispatch' | 'clear' | 'transfer' | 'present'
        self.outputs = tuple(o for o in outputs if o is not None)
        self.depth_out = depth_out
        self.marker_path = tuple(marker_path)
        self.name = name
        self.copy_src = copy_src
        self.copy_dst = copy_dst

    def targets(self):
        t = set(self.outputs)
        if self.depth_out is not None:
            t.add(self.depth_out)
        if self.copy_dst is not None:
            t.add(self.copy_dst)
        return t


class PassNode(object):
    def __init__(self, order, kind, name, leaves, marker_path,
                 collapsed_frame=False):
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

    @property
    def frame_path(self):
        return self.marker_path[:-1]


class ResourceVersionNode(object):
    def __init__(self, res_key, name, version, res_kind, info, imported=False):
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
        self.scope_input = False   # written outside the scope, read inside
        self.internal = False      # content never leaves its single toucher

    def label(self):
        if getattr(self, 'version_count', self.version) >= 2:
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


def _is_shader_usage(uname):
    """Shader-binding usages the descriptor scan can judge; fixed-function
    usages (attachments, copies, clears) are never 'unused'."""
    return uname.endswith('_Resource') or uname.endswith('_RWResource')


def apply_binding_usage(fg, results):
    """Dash a read edge only when ALL its shader-usage events are confirmed
    unused; any used / fixed-function / pending event keeps it solid."""
    res_by_id = dict((n.id, n) for n in fg.resources)
    for e in fg.edges:
        if e.kind != READ:
            continue
        node = res_by_id.get(e.src_id)
        if node is None or getattr(node, 'bundle_members', None):
            continue
        verdicts = []
        for eid, uname in e.usages:
            if not _is_shader_usage(uname):
                verdicts = None  # fixed-function event: always "used"
                break
            verdicts.append(results.get((eid, node.res_key)))
        e.unused_binding = (bool(verdicts) and
                            all(v == 'unused' for v in verdicts))
    return fg


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
    """Markerless grouping: consecutive draws sharing (outputs, depth_out)
    merge; consecutive dispatches merge; clears/transfers stand alone."""
    groups = []
    for act in leaves:
        key = None
        if act.kind == KIND_DRAW:
            key = (KIND_DRAW, act.outputs, act.depth_out)
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
                t = set(a.outputs)
                if a.depth_out is not None:
                    t.add(a.depth_out)
                return t
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
                rt = primary.outputs[0] if primary.outputs else primary.depth_out
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


def _bucket_usages(passes, usage_by_res):
    """Bucket usage events into pass intervals; gap events (renderpass
    boundary ops) attach to a neighbour: Clear/Discard -> next pass (loadOp),
    attachment/resolve -> prev pass (storeOp), else nearest (tie -> next).
    Events before the first / after the last pass are dropped.

    -> {res_key: {pass_index: {'r': [(eid, uname)], 'w': [(eid, uname)]}}}
    """
    firsts = [p.first_eid for p in passes]

    def find_pass(eid, uname):
        i = bisect.bisect_right(firsts, eid) - 1
        if 0 <= i and passes[i].first_eid <= eid <= passes[i].last_eid:
            return i
        if 0 <= i and i + 1 < len(passes):  # gap between pass i and i+1
            if uname in ('Clear', 'Discard'):  # loadOp
                return i + 1
            if uname in ('ColorTarget', 'DepthStencilTarget',
                         'ResolveSrc', 'ResolveDst'):  # storeOp
                return i
            prev_d = eid - passes[i].last_eid
            next_d = passes[i + 1].first_eid - eid
            return i + 1 if next_d <= prev_d else i
        return None

    slots = {}
    for res_key in sorted(usage_by_res.keys()):
        per_pass = {}
        for eid, uname in sorted(usage_by_res[res_key]):
            acc = USAGE_ACCESS.get(uname, IGNORE)
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


def build_graph(passes, usage_by_res, res_info, res_names=None, versioned=False,
                externally_written=None):
    """Build the bipartite pass/resource graph from bucketed usage events.

    Merged mode (default): one node per resource; fg.rank_edges is a DAG-safe
    set for layout (drawn edges may flow backwards in time). versioned=True
    splits into version nodes on write-after-read so each read points at its
    producing write. Pass aggregation is the caller's (build_passes).

    externally_written: res_keys written OUTSIDE this view (e.g. elsewhere in
    the frame when scoped).
    usage_by_res: {res_key: [(eventId, usage_name), ...]} any order.
    res_info:     {res_key: {'kind': str, 'info': dict}}.
    """
    res_names = res_names or {}
    externally_written = externally_written or set()
    fg = FrameGraph()

    slots = _bucket_usages(passes, usage_by_res)

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

    # GetUsage reports nothing for present events; link backbuffer via copySource.
    for p in fg.passes:
        if p.kind != CAT_PRESENT:
            continue
        for leaf in p.leaves:
            src = leaf.copy_src
            if src is None or src not in res_info:
                continue
            node = latest.get(src)
            if node is None:
                node = new_version(src, imported=True)
                latest[src] = node
            add_edge(node.id, p.id, READ, [(leaf.eid, 'Present')])
            if p.id not in node.reader_ids:
                node.reader_ids.append(p.id)
            if not versioned:
                fg.rank_edges.append(Edge(node.id, p.id, EDGE_RANK))

    if versioned:
        fg.rank_edges = list(fg.edges)  # versioned construction is already a DAG

    # Stamp each node with its res_key's total write-version count (UI badge).
    for node in fg.resources:
        node.version_count = version_count.get(node.res_key, node.version)

    # Orphan passes (no candidate-resource I/O): UI hides them; rank hint after
    # the nearest preceding non-orphan keeps them placed when shown.
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

    # ---- nested frames: frame paths + each resource's lowest common frame
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

    # internal = pure self-read-write (one toucher both reads and writes a
    # private working set; the read may have been folded, hence folded_self_rw).
    # Write-only is NOT internal: produced-but-unread content stays visible so
    # the user can judge readback-vs-bug (also exposes a present-less swapchain).
    for node in fg.resources:
        touchers = set(node.writer_ids) | set(node.reader_ids)
        self_rw = (bool(node.reader_ids) or
                   node.res_key in folded_self_rw)
        node.internal = (bool(node.writer_ids) and self_rw and
                         len(touchers) == 1)
    return fg


# ----------------------------------------------------------------- runtime
# Everything below touches renderdoc (imported lazily) and runs on the
# replay thread only.

def _collect_leaves(rd, roots, sdfile, key_of):
    """Flatten the action tree into LeafAction IR. Recurse into PushMarker
    regions and grouping nodes; MultiAction stays a single leaf. Only semantic
    debug markers contribute to marker_path - API-structure groupings
    (vkQueueSubmit, render-pass regions, command buffers) are recursed but
    excluded so pass names stay meaningful.
    """
    def flag(name):
        return getattr(rd.ActionFlags, name, 0)

    f_draw = flag('Drawcall') | flag('MeshDispatch')
    f_dispatch = flag('Dispatch') | flag('DispatchRay')
    f_clear = flag('Clear')
    f_transfer = flag('Copy') | flag('Resolve') | flag('GenMips')
    f_present = flag('Present')
    f_push = flag('PushMarker')
    f_multi = flag('MultiAction')
    f_structural = (flag('CmdList') | flag('PassBoundary') | flag('BeginPass') |
                    flag('EndPass') | flag('CommandBufferBoundary'))

    leaves = []

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
        if children and not (f & f_multi) and (kind is None or (f & f_push)):
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
        leaves.append(LeafAction(
            act.eventId, kind,
            outputs=outs,
            depth_out=key_of(act.depthOut),
            marker_path=path,
            name=action_name(act),
            copy_src=key_of(act.copySource),
            copy_dst=key_of(act.copyDestination)))

    for a in roots:
        visit(a, ())
    return leaves


def texture_kind_of(cf, texcat, cands=None):
    """Candidate gate + node-kind for a texture's creationFlags; None when the
    config excludes it. texcat=rd.TextureCategory (param'd for testing).

    Admission ORs enabled class masks; 'tex_other' admits textures with none of
    the four classic flags (sampled/staging) without re-admitting a turned-off
    class. Classification is gate-independent (priority swap > depth > color > rw)."""
    if cands is None:
        cands = _config.DEFAULTS
    mask = 0
    if cands.get(_config.KEY_TEX_COLOR):
        mask |= texcat.ColorTarget
    if cands.get(_config.KEY_TEX_DEPTH):
        mask |= texcat.DepthTarget
    if cands.get(_config.KEY_TEX_RW):
        mask |= texcat.ShaderReadWrite
    if cands.get(_config.KEY_TEX_SWAP):
        mask |= texcat.SwapBuffer
    classic = (texcat.ColorTarget | texcat.DepthTarget |
               texcat.ShaderReadWrite | texcat.SwapBuffer)
    if not (cf & mask) and not (cands.get(_config.KEY_TEX_OTHER) and
                                not (cf & classic)):
        return None
    if cf & texcat.SwapBuffer:
        return RES_SWAPCHAIN
    if cf & texcat.DepthTarget:
        return RES_DEPTH
    if cf & texcat.ColorTarget:
        return RES_COLOR
    if cf & texcat.ShaderReadWrite:
        return RES_UAV_TEX
    return RES_SAMPLED


def buffer_admitted(cf, bufcat, cands=None):
    """Candidate gate for a buffer's creationFlags. bufcat=rd.BufferCategory.
    'buf_noflags' admits creationFlags==0 buffers (copy dst / readback staging,
    invisible to every category mask)."""
    if cands is None:
        cands = _config.DEFAULTS
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


# Vulkan enum values (stable per the VK spec)
_VK_COMPARE_OP_ALWAYS = 7
_VK_STENCIL_OP_KEEP = 0
_VK_BIND_POINT_GRAPHICS = 0

def _label_flag_masks(rd):
    """(marker_mask, exclude_mask) for API-agnostic debug-label detection: an
    action whose flags are PURELY marker-class executes nothing, so any usage
    on it is a phantom. exclude keeps structural/executing actions (which can
    also carry PopMarker) out of the label set."""
    def fl(name):
        return getattr(rd.ActionFlags, name, 0)
    marker = fl('PushMarker') | fl('PopMarker') | fl('SetMarker')
    exclude = (fl('CmdList') | fl('PassBoundary') | fl('BeginPass') |
               fl('EndPass') | fl('CommandBufferBoundary') |
               fl('MultiAction') | fl('Drawcall') | fl('Dispatch') |
               fl('MeshDispatch') | fl('DispatchRay') | fl('Clear') |
               fl('Copy') | fl('Resolve') | fl('GenMips') |
               fl('Present'))
    return marker, exclude


def _last_resource_id(ch):
    """Output-parameter lookup: outputs serialise after inputs, so the LAST
    non-null resource-id child is the created/bound object."""
    rid = None
    for i in range(ch.NumChildren()):
        try:
            r = ch.GetChild(i).AsResourceId()
        except Exception:
            continue
        s = _rid_str(r)
        if s is not None:
            rid = s
    return rid


def _chunk_is(name, tail):
    """Chunk-name family match: bare name or namespaced '...::tail' (the depth
    adapters recognise chunk families regardless of API prefix)."""
    return name == tail or name.endswith('::' + tail)


def _pipeline_depth_access(dss):
    """Classify one VkPipelineDepthStencilStateCreateInfo into read/write/rw/none.
    A test with compareOp != ALWAYS samples the buffer, hence 'read'."""
    if dss is None or dss.NumChildren() == 0:
        return ACCESS_NONE   # NULL state: no depth attachment
    reads = False
    writes = False
    if _child_int(dss, 'depthTestEnable'):
        if _child_int(dss, 'depthCompareOp') != _VK_COMPARE_OP_ALWAYS:
            reads = True
        if _child_int(dss, 'depthWriteEnable'):
            writes = True
    if _child_int(dss, 'stencilTestEnable'):
        for fname in ('front', 'back'):
            f = dss.FindChild(fname)
            if f is None:
                continue
            if _child_int(f, 'compareOp') != _VK_COMPARE_OP_ALWAYS:
                reads = True
            wm = _child_int(f, 'writeMask')
            ops = (_child_int(f, 'failOp'), _child_int(f, 'passOp'),
                   _child_int(f, 'depthFailOp'))
            if wm and any(o != _VK_STENCIL_OP_KEEP for o in ops):
                writes = True
    if writes:
        return RW if reads else WRITE
    return READ if reads else ACCESS_NONE


# ---- per-API depth-state adapters ------------------------------------ #
# Only "where the baked depth/stencil state lives" is per-API (abstract
# PipeState exposes none). ONLY VULKAN AND D3D12 ARE VALIDATED; D3D11/GL
# follow RenderDoc serialisation conventions, parse defensively, and a
# creation family that yields nothing raises a VISIBLE warning rather than
# silently refining nothing.

_D3D_COMPARISON_ALWAYS = 8     # D3D11/12_COMPARISON_FUNC_ALWAYS
_D3D_STENCIL_OP_KEEP = 1       # D3D11/12_STENCIL_OP_KEEP
_GL_DEPTH_TEST = 0x0B71
_GL_STENCIL_TEST = 0x0B90
_GL_ALWAYS = 0x0207
_GL_LESS = 0x0201
_GL_KEEP = 0x1E00


def _d3d_depth_access(dss):
    """Classify a D3D depth/stencil desc into read/write/rw/none. D3D11,
    classic D3D12 and stream-PSO DESC2 share field names; only the stencil
    write mask differs (DESC2 per-face vs classic top-level), handled by the
    per-face fallback below."""
    if dss is None:
        return None            # unknown: keep legacy WRITE
    reads = False
    writes = False
    if _child_int(dss, 'DepthEnable'):
        if _child_int(dss, 'DepthFunc') != _D3D_COMPARISON_ALWAYS:
            reads = True
        if _child_int(dss, 'DepthWriteMask'):   # ZERO=0 / ALL=1
            writes = True
    if _child_int(dss, 'StencilEnable'):
        top_wm = _child_int(dss, 'StencilWriteMask')   # classic / D3D11
        for fname in ('FrontFace', 'BackFace'):
            f = dss.FindChild(fname)
            if f is None:
                continue
            if _child_int(f, 'StencilFunc') != _D3D_COMPARISON_ALWAYS:
                reads = True
            wm = _child_int(f, 'StencilWriteMask', top_wm)   # DESC2: per-face
            ops = (_child_int(f, 'StencilFailOp', _D3D_STENCIL_OP_KEEP),
                   _child_int(f, 'StencilDepthFailOp',
                              _D3D_STENCIL_OP_KEEP),
                   _child_int(f, 'StencilPassOp', _D3D_STENCIL_OP_KEEP))
            if wm and any(o != _D3D_STENCIL_OP_KEEP for o in ops):
                writes = True
    if writes:
        return RW if reads else WRITE
    return READ if reads else ACCESS_NONE


class _VkDepthAdapter(object):
    """Validated against a real Vulkan capture."""

    @staticmethod
    def build_tables(chunks):
        tables = {}
        seen = 0
        for c in chunks:
            if c.name != 'vkCreateGraphicsPipelines':
                continue
            seen += 1
            try:
                pid = str(c.FindChild('Pipeline').AsResourceId())
                info = c.FindChild('CreateInfo')
                dss = (info.FindChild('pDepthStencilState')
                       if info is not None else None)
                tables[pid] = _pipeline_depth_access(dss)
            except Exception:
                continue
        return tables, seen

    @staticmethod
    def on_chunk(ch, state, tables):
        if ch.name != 'vkCmdBindPipeline':
            return
        try:
            if (ch.FindChild('pipelineBindPoint').AsInt() ==
                    _VK_BIND_POINT_GRAPHICS):
                state['cur'] = str(
                    ch.FindChild('pipeline').AsResourceId())
        except Exception:
            pass

    @staticmethod
    def current(state, tables):
        return tables.get(state.get('cur'))


class _D3D12DepthAdapter(object):
    """Parses the graphics-PSO depth-stencil state. RenderDoc names the chunk
    'CreateGraphicsPipeline' for a classic CreateGraphicsPipelineState call,
    'CreatePipelineState' for a stream-style one (modern RHIs / UE5 emit the
    latter); pDesc.DepthStencilState carries the same field names either way."""

    @staticmethod
    def build_tables(chunks):
        tables = {}
        seen = 0
        for c in chunks:
            if not (_chunk_is(c.name, 'CreateGraphicsPipeline')
                    or _chunk_is(c.name, 'CreateGraphicsPipelineState')
                    or _chunk_is(c.name, 'CreatePipelineState')):
                continue
            seen += 1
            try:
                desc = c.FindChild('pDesc')
                dss = (desc.FindChild('DepthStencilState')
                       if desc is not None else None)
                pid = None
                for nm in ('pPipelineState', 'pPipeline',
                           'PipelineState'):
                    n = c.FindChild(nm)
                    if n is not None:
                        pid = str(n.AsResourceId())
                        break
                if pid is None:
                    pid = _last_resource_id(c)
                acc = _d3d_depth_access(dss)
                if pid and acc is not None:
                    tables[pid] = acc
            except Exception:
                continue
        return tables, seen

    @staticmethod
    def on_chunk(ch, state, tables):
        if not _chunk_is(ch.name, 'SetPipelineState'):
            return
        try:
            n = ch.FindChild('pPipelineState')
            state['cur'] = (str(n.AsResourceId()) if n is not None
                            else _last_resource_id(ch))
        except Exception:
            pass

    @staticmethod
    def current(state, tables):
        return tables.get(state.get('cur'))


class _D3D11DepthAdapter(object):
    """UNVALIDATED. Depth state is a context-bound object; unbound/NULL means
    the API default (test on, write all, func LESS = read-write)."""

    _DEFAULT = RW

    @staticmethod
    def build_tables(chunks):
        tables = {}
        seen = 0
        for c in chunks:
            if not _chunk_is(c.name, 'CreateDepthStencilState'):
                continue
            seen += 1
            try:
                desc = c.FindChild('pDepthStencilDesc')
                if desc is None:   # fall back: child carrying the desc fields
                    for i in range(c.NumChildren()):
                        k = c.GetChild(i)
                        if k.FindChild('DepthEnable') is not None:
                            desc = k
                            break
                out = c.FindChild('ppDepthStencilState')
                pid = (str(out.AsResourceId()) if out is not None
                       else _last_resource_id(c))
                acc = _d3d_depth_access(desc)
                if pid and acc is not None:
                    tables[pid] = acc
            except Exception:
                continue
        return tables, seen

    @staticmethod
    def on_chunk(ch, state, tables):
        if not _chunk_is(ch.name, 'OMSetDepthStencilState'):
            return
        try:
            n = ch.FindChild('pDepthStencilState')
            pid = (str(n.AsResourceId()) if n is not None
                   else _last_resource_id(ch))
            state['cur'] = _rid_str(pid)
        except Exception:
            pass

    @staticmethod
    def current(state, tables):
        pid = state.get('cur')
        if pid is None:
            return _D3D11DepthAdapter._DEFAULT
        return tables.get(pid)   # unknown object: legacy WRITE


class _GLDepthAdapter(object):
    """UNVALIDATED. GL has no state objects - discrete calls mutate the
    context; *Separate stencil variants are coarsened (last call wins)."""

    @staticmethod
    def build_tables(chunks):
        return {}, 0    # nothing to create; never reports parse failure

    @staticmethod
    def on_chunk(ch, state, tables):
        name = ch.name
        if name in ('glEnable', 'glDisable'):
            cap = _child_int(ch, 'cap', -1)
            on = (name == 'glEnable')
            if cap == _GL_DEPTH_TEST:
                state['dtest'] = on
            elif cap == _GL_STENCIL_TEST:
                state['stest'] = on
        elif name == 'glDepthMask':
            state['dwrite'] = bool(_child_int(ch, 'flag', 1))
        elif name == 'glDepthFunc':
            state['dfunc'] = _child_int(ch, 'func', _GL_LESS)
        elif name in ('glStencilOp', 'glStencilOpSeparate'):
            ops = (_child_int(ch, 'sfail', _GL_KEEP),
                   _child_int(ch, 'dpfail', _GL_KEEP),
                   _child_int(ch, 'dppass', _GL_KEEP))
            state['sop_write'] = any(o != _GL_KEEP for o in ops)
        elif name in ('glStencilFunc', 'glStencilFuncSeparate'):
            state['sfunc'] = _child_int(ch, 'func', _GL_ALWAYS)
        elif name in ('glStencilMask', 'glStencilMaskSeparate'):
            state['smask'] = _child_int(ch, 'mask', 0xFF)

    @staticmethod
    def current(state, tables):
        reads = False
        writes = False
        if state.get('dtest', False):     # GL default: test disabled
            if state.get('dfunc', _GL_LESS) != _GL_ALWAYS:
                reads = True
            if state.get('dwrite', True):
                writes = True
        if state.get('stest', False):
            if state.get('sfunc', _GL_ALWAYS) != _GL_ALWAYS:
                reads = True
            if state.get('smask', 0xFF) and state.get('sop_write',
                                                      False):
                writes = True
        if writes:
            return RW if reads else WRITE
        return READ if reads else ACCESS_NONE


_DEPTH_ADAPTERS = {
    'vulkan': _VkDepthAdapter,
    'd3d12': _D3D12DepthAdapter,
    'd3d11': _D3D11DepthAdapter,
    'opengl': _GLDepthAdapter,
}


def _refine_depth_access(controller, rd, usage_by_res, leaf_eids,
                         progress=None, warnings=None):
    """Classify each depth-attachment binding from the bound pipeline's BAKED
    state, read statically from structured data - ZERO replay round-trips (the
    old SetFrameEvent walk cost ~86ms/event, ~55s/capture). Build a
    {pipeline: access} table, then walk the action tree in event order tracking
    the current pipeline so each depth-bound draw inherits its access.

    Only executable leaves are classified; boundary events (store/resolve) keep
    legacy WRITE. Dynamic depth state is not rewritten (draws fall back to baked
    state). Dispatches through the per-API adapter registry; unknown APIs skip.

    Returns (access, label_eids): access = {eid: read|write|rw|none}, missing =
    legacy WRITE; label_eids = debug-label events whose phantom attachment
    usages the caller strips."""
    try:
        roots = controller.GetRootActions()
    except Exception:
        return {}, set()
    f_marker, f_not_label = _label_flag_masks(rd)

    adapter = None
    chunks = None
    api_key = ''
    try:
        api_key = str(controller.GetAPIProperties()
                      .pipelineType).split('.')[-1].lower()
        adapter = _DEPTH_ADAPTERS.get(api_key)
        if adapter is not None:
            chunks = controller.GetStructuredFile().chunks
    except Exception:
        adapter = None

    targets = set()
    if adapter is not None:
        for evs in usage_by_res.values():
            for (eid, uname) in evs:
                if uname == 'DepthStencilTarget' and eid in leaf_eids:
                    targets.add(eid)
    if progress is not None:
        progress(0, len(targets))

    tables = {}
    if adapter is not None:
        try:
            tables, seen_create = adapter.build_tables(chunks)
        except Exception:
            tables, seen_create = {}, 0
        if seen_create and not tables:
            # chunk family exists but nothing parsed (unexpected layout): warn
            # rather than silently refine nothing
            if warnings is not None:
                warnings.append(
                    'depth refinement: %s pipeline parsing failed '
                    '(unvalidated adapter) - depth bindings keep '
                    'write semantics' % api_key)
            adapter = None

    access = {}
    label_eids = set()
    state = {}

    def visit(acts):
        for a in acts:
            if (a.flags & f_marker) and not (a.flags & f_not_label):
                label_eids.add(a.eventId)
            if adapter is not None:
                for ev in a.events:
                    try:
                        ch = chunks[ev.chunkIndex]
                    except Exception:
                        continue
                    adapter.on_chunk(ch, state, tables)
                if a.eventId in targets:
                    acc = adapter.current(state, tables)
                    if acc is not None:
                        access[a.eventId] = acc
            visit(a.children)

    visit(roots)
    if progress is not None:
        progress(len(targets), len(targets))
    return access, label_eids


def _strip_label_usages(usage_by_res, label_eids):
    """Drop usages on debug-label events: they execute nothing, so the
    attachment usages RenderDoc tags on them are phantoms that would forge a
    writer for a pass that never touched the resource."""
    if not label_eids:
        return
    for res_key, evs in usage_by_res.items():
        out = [(eid, uname) for (eid, uname) in evs
               if eid not in label_eids]
        if len(out) != len(evs):
            usage_by_res[res_key] = out


DROP = object()   # rename-table sentinel: delete the event entirely


def _apply_access_rename(usage_by_res, lookup, usage_filter, rename):
    """Rewrite usage names in place from an access verdict. lookup(eid, res_key)
    -> access (None leaves the event alone); usage_filter(uname) picks usages to
    consider; rename maps access -> new uname (str or callable(old)->new), or
    DROP to delete the event. Shared by depth and shader-access refinement."""
    for res_key, evs in list(usage_by_res.items()):
        out = []
        changed = False
        for (eid, uname) in evs:
            if usage_filter(uname):
                a = lookup(eid, res_key)
                repl = rename.get(a) if a is not None else None
                if repl is DROP:
                    changed = True
                    continue
                if repl is not None:
                    uname = repl(uname) if callable(repl) else repl
                    changed = True
            out.append((eid, uname))
        if changed:
            usage_by_res[res_key] = out


def _apply_depth_access(usage_by_res, access):
    """RenderDoc reports every depth binding as write-class DepthStencilTarget;
    rewrite test-only / test+write bindings per draw, drop no-attachment draws."""
    if not access:
        return
    _apply_access_rename(
        usage_by_res,
        lookup=lambda eid, rk: access.get(eid),
        usage_filter=lambda u: u == 'DepthStencilTarget',
        rename={READ: 'DepthTestRead', RW: 'DepthTestRW', ACCESS_NONE: DROP})


def _apply_shader_access(usage_by_res, shader_access):
    """Refine *_RWResource per (eid, res_key) from shader parsing: read-only and
    write-only get renamed so _bucket_usages drops the spurious edge; rw / unused
    stay as RWResource (rw keeps both edges; unused stays solid here and is dashed
    separately by apply_binding_usage)."""
    if not shader_access:
        return

    def to_read(u):
        return u[:-len('RWResource')] + 'ReadResource'

    def to_write(u):
        return u[:-len('RWResource')] + 'WriteResource'

    _apply_access_rename(
        usage_by_res,
        lookup=lambda eid, rk: shader_access.get((eid, rk)),
        usage_filter=lambda u: u.endswith('_RWResource'),
        rename={READ: to_read, WRITE: to_write})


def extract_bundle(controller, include_buffers=True, candidates=None,
                   depth_access=None, progress=None):
    """Pull actions + per-resource usage into a plain-Python bundle. Replay
    thread only. The graph is built from the bundle on the UI side so
    marker-depth/versioning toggles re-render without another replay.

    candidates: config.candidates_of()-shaped dict gating which resources enter
    (None = defaults). include_buffers gates the buffer pass as a whole.
    depth_access: cached {'access', 'labels'} from a prior extraction of the
    SAME capture (eids are stable) to skip the structured-data walk; None =
    walk and return it. progress(done, total) reports the walk."""
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
    leaves = _collect_leaves(rd, controller.GetRootActions(), sdfile, key_of)

    if depth_access is None:
        leaf_eids = set(l.eid for l in leaves)
        access, labels = _refine_depth_access(
            controller, rd, usage_by_res, leaf_eids, progress=progress,
            warnings=warnings)
        depth_access = {'access': access, 'labels': sorted(labels)}
    _apply_depth_access(usage_by_res, depth_access.get('access') or {})
    _strip_label_usages(usage_by_res,
                        frozenset(depth_access.get('labels') or ()))

    return {
        'leaves': leaves,
        'usage_by_res': usage_by_res,
        'res_info': res_info,
        'res_names': res_names,
        'rid_objects': rid_objects,
        'depth_access': depth_access,
        'warnings': warnings,
        'seconds': time.time() - t0,
    }


LARGE_FRAME_PASS_WARN = 2000   # pass-node count above which the UI warns


def build_from_bundle(bundle, marker_depth=None, versioned=False,
                      collapsed=None):
    """Build the FrameGraph from a bundle. Pure Python, any thread."""
    passes = build_passes(bundle['leaves'], bundle['res_names'], marker_depth,
                          collapsed=collapsed)
    fg = build_graph(passes, bundle['usage_by_res'], bundle['res_info'],
                     bundle['res_names'], versioned=versioned)
    fg.warnings = list(bundle['warnings']) + fg.warnings
    fg.rid_objects = bundle['rid_objects']
    fg.stats = {
        'passes': len(fg.passes),
        'resources': len(fg.resources),
        'edges': len(fg.edges),
        'seconds': bundle['seconds'],
    }
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
            # producer -> imported head only from writes BEFORE the scope
            # (a later write is a different version, not this input)
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


def build_scoped(bundle, scope_path, scope_range, versioned=True,
                 bundling=False, make_portals=True, shader_access=None):
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

    usage = bundle['usage_by_res']
    if shader_access:
        # shader access is async/incremental -> apply to a copy, never the bundle
        usage = dict((k, list(v)) for k, v in usage.items())
        _apply_shader_access(usage, shader_access)
    scoped_usage = {}
    outside = {}
    outside_events = {}
    for res_key, evs in usage.items():
        inside_evs = []
        out_r = 0
        out_w = 0
        for eid, uname in evs:
            acc = USAGE_ACCESS.get(uname, IGNORE)
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

    # Present has no GetUsage event; count an out-of-scope present as an
    # external reader so the swapchain isn't misclassified as internal.
    for leaf in bundle['leaves']:
        if leaf.kind != KIND_PRESENT or leaf.copy_src is None:
            continue
        if scope_range is not None and (
                scope_range[0] <= leaf.eid <= scope_range[1]):
            continue
        res_key = leaf.copy_src
        if res_key not in bundle['res_info']:
            continue
        out_r, out_w = outside.get(res_key, (0, 0))
        outside[res_key] = (out_r + 1, out_w)
        outside_events.setdefault(res_key, []).append(
            (leaf.eid, 'Present', READ))

    fg = build_graph(passes, scoped_usage, bundle['res_info'],
                     bundle['res_names'], versioned=versioned,
                     externally_written=set(
                         k for k, (_r, w) in outside.items() if w > 0))
    # outside-writer identity: eids writing each resource beyond this scope.
    # Inputs fed by DIFFERENT external writers are different behaviours and
    # must not bundle even though the in-view writer set collapses to empty.
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

    # ---- bundling in the parse layer (before portals) so every consumer -
    # graph, portal targets, jump focus - sees ONE merged result
    if bundling:
        bundle_equivalent_resources(fg)
        bundle_equivalent_passes(fg)

    # ---- portals for external scopes: each outside event maps to the instance
    # one level below the common prefix with this scope; producers feed the
    # imported head episode, the tail feeds consumers. Double-click jumps there.
    if make_portals and scope_range is not None and outside_events:
        _attach_scope_portals(fg, bundle, scope_path, scope_range, level,
                              outside_events, versioned, bundling)

    fg.warnings = list(bundle['warnings']) + fg.warnings
    fg.rid_objects = bundle['rid_objects']
    fg.stats = {
        'passes': len(fg.passes),
        'resources': len(fg.resources),
        'edges': len(fg.edges),
        'seconds': bundle['seconds'],
    }
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
    fg.edges = _remap_edges(fg.edges, remap)
    fg.rank_edges = _remap_edges(fg.rank_edges, remap)
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
    fg.edges = _remap_edges(fg.edges, remap)
    fg.rank_edges = _remap_edges(fg.rank_edges, remap)
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


_PASS_SUFFIX_RE = re.compile(r'\s+#\d+$')


def bundle_equivalent_passes(fg, min_members=BUNDLE_MIN_MEMBERS):
    """PASS bundling, the mirror of resource bundling. Leaf-level passes merge
    when edge structure is identical (kind, read set, written set) and names
    are similar; the auto-bucket ' #N' suffix is stripped before signing.
    Drillable/portal/present nodes never merge. Run AFTER
    bundle_equivalent_resources so groups compare merged resource ids. The
    first member is mutated into the bundle (id and edges reused);
    bundle_members/bundle_member_eids are parallel lists for display and jumps."""
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

    # merged passes must be CONSECUTIVE in execution order: split each group
    # into consecutive runs and only merge runs still reaching the threshold
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
    fg.edges = _remap_edges(fg.edges, remap)
    fg.rank_edges = _remap_edges(fg.rank_edges, remap)
    if getattr(fg, 'orphan_pass_ids', None):
        fg.orphan_pass_ids = set(
            remap.get(i, i) for i in fg.orphan_pass_ids)
    return fg


def build_frame_graph(controller, include_buffers=True, versioned=False,
                      marker_depth=None, candidates=None):
    """Convenience for scripts: extract + build in one call. Replay thread."""
    return build_from_bundle(
        extract_bundle(controller, include_buffers, candidates=candidates),
        marker_depth, versioned)
