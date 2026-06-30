# -*- coding: utf-8 -*-
"""Scene IR + YAML loader/validator for the render-graph test harness.

Runs in system Python (pyyaml available); targets Python 3.6 (no dataclasses).
A scene YAML describes a render graph at the graph-abstraction level; the loader
turns it into a validated Scene object that the codegen / manifest / oracle all
consume.
"""

# --- resource kinds (mirror graph_model res_kind vocabulary) ---
KIND_BUFFER = 'buffer'
KIND_UAV_TEX = 'uav_tex'
KIND_COLOR = 'color'
KIND_DEPTH = 'depth'
KIND_SAMPLED = 'sampled'
KIND_SWAPCHAIN = 'swapchain'
KIND_CBUFFER = 'cbuffer'    # constant/uniform buffer (bound through a cbv slot)
KIND_VBUFFER = 'vbuffer'    # vertex buffer (input-assembler stream)
KIND_IBUFFER = 'ibuffer'    # index buffer (indexed draw)
RES_KINDS = {KIND_BUFFER, KIND_UAV_TEX, KIND_COLOR, KIND_DEPTH,
             KIND_SAMPLED, KIND_SWAPCHAIN, KIND_CBUFFER,
             KIND_VBUFFER, KIND_IBUFFER}

# --- binding kinds (what is bound to a shader slot) ---
BIND_SRV_BUF = 'srv_buf'
BIND_UAV_BUF = 'uav_buf'
BIND_UAV_TEX = 'uav_tex'
BIND_SAMPLED = 'sampled'
BIND_CBV = 'cbv'
BIND_KINDS = {BIND_SRV_BUF, BIND_UAV_BUF, BIND_UAV_TEX, BIND_SAMPLED, BIND_CBV}
# bindings the shader can write through (UAVs); the rest are read-only by API
UAV_BINDS = {BIND_UAV_BUF, BIND_UAV_TEX}

# --- actual shader access (what the bytecode does to a binding) ---
ACC_READ = 'read'
ACC_WRITE = 'write'
ACC_RW = 'rw'
ACC_NONE = 'none'          # bound but never accessed -> UNUSED in the viewer
ACCESSES = {ACC_READ, ACC_WRITE, ACC_RW, ACC_NONE}

# --- pass types -> graph_model pass categories ---
PASS_COMPUTE = 'compute'
PASS_GRAPHICS = 'graphics'
PASS_TRANSFER = 'transfer'
PASS_PRESENT = 'present'
PASS_TYPES = {PASS_COMPUTE, PASS_GRAPHICS, PASS_TRANSFER, PASS_PRESENT}


class SchemaError(Exception):
    pass


class Resource(object):
    __slots__ = ('name', 'kind', 'dims', 'fmt', 'elements')

    def __init__(self, name, kind, dims=None, fmt=None, elements=None):
        self.name = name
        self.kind = kind
        self.dims = dims          # (w, h) for textures, else None
        self.fmt = fmt            # format token, e.g. 'rgba32f', 'd32'
        self.elements = elements  # element count for buffers

    def __repr__(self):
        return 'Resource(%s, %s)' % (self.name, self.kind)


class Binding(object):
    __slots__ = ('res', 'bind', 'access', 'atomic')

    def __init__(self, res, bind, access, atomic=False):
        self.res = res
        self.bind = bind
        self.access = access
        self.atomic = atomic   # realize an rw UAV via InterlockedAdd

    def __repr__(self):
        return 'Binding(%s, %s, %s)' % (self.res, self.bind, self.access)


class Pass(object):
    __slots__ = ('name', 'type', 'scope', 'groups', 'binds', 'color', 'depth',
                 'sample', 'vertex', 'index', 'copy', 'swapchain', 'repeat',
                 'marker')

    def __init__(self, name, type, scope=None, groups=1, binds=None, color=None,
                 depth=None, sample=None, vertex=None, index=None, copy=None,
                 swapchain=None, repeat=1, marker=True):
        self.name = name
        self.type = type
        self.scope = list(scope or [])        # marker path; [] = root
        self.marker = marker                  # False -> no debug label (markerless)
        self.groups = groups                  # compute dispatch groups
        self.binds = list(binds or [])        # list[Binding] (compute UAV/SRV)
        self.color = list(color or [])        # graphics color attachment res names
        self.depth = depth                    # {'res':..,'access':read|write|rw,
        #                                         'store':store|dont_care} or None
        self.sample = list(sample or [])      # list[Binding] (graphics PS SRV samples)
        self.vertex = list(vertex or [])      # vbuffer res names, one per IA stream
        self.index = index                    # ibuffer res name (indexed draw) or None
        self.copy = copy                      # {'src':..,'dst':..} or None
        self.swapchain = swapchain            # present target res name or None
        self.repeat = repeat                  # >=1 structurally-equivalent instances

    def __repr__(self):
        return 'Pass(%s, %s)' % (self.name, self.type)


class Scene(object):
    __slots__ = ('name', 'resources', 'passes', 'bundling', 'frame_prior')

    def __init__(self, name, resources, passes, bundling=False, frame_prior=False):
        self.name = name
        self.resources = resources    # dict name -> Resource
        self.passes = passes          # list[Pass]
        self.bundling = bundling      # build the graph with equivalent-node bundling
        # descriptors/views are created outside the captured frame
        self.frame_prior = frame_prior

    def __repr__(self):
        return 'Scene(%s, %d res, %d passes)' % (
            self.name, len(self.resources), len(self.passes))


def _binding(d, where):
    if not isinstance(d, dict) or 'res' not in d or 'bind' not in d:
        raise SchemaError('%s: binding needs res+bind, got %r' % (where, d))
    bind = d['bind']
    access = d.get('access', ACC_READ)
    if bind not in BIND_KINDS:
        raise SchemaError('%s: unknown bind %r' % (where, bind))
    if access not in ACCESSES:
        raise SchemaError('%s: unknown access %r' % (where, access))
    if bind not in UAV_BINDS and access in (ACC_WRITE, ACC_RW):
        raise SchemaError('%s: %s cannot be %s (only UAVs are writable)'
                          % (where, bind, access))
    atomic = bool(d.get('atomic', False))
    if atomic and (bind != BIND_UAV_BUF or access != ACC_RW):
        raise SchemaError('%s: atomic only valid on an rw uav_buf' % where)
    return Binding(d['res'], bind, access, atomic=atomic)


def load_dict(data):
    """Build + validate a Scene from a parsed dict (the YAML body)."""
    if not isinstance(data, dict):
        raise SchemaError('scene must be a mapping')
    name = data.get('name', 'scene')
    resources = {}
    for rn, rd in (data.get('resources') or {}).items():
        rd = rd or {}
        kind = rd.get('kind')
        if kind not in RES_KINDS:
            raise SchemaError('resource %s: unknown kind %r' % (rn, kind))
        dims = rd.get('size')
        if dims is not None:
            dims = tuple(dims)
        resources[rn] = Resource(rn, kind, dims=dims, fmt=rd.get('format'),
                                 elements=rd.get('elements'))

    passes = []
    for pd in (data.get('passes') or []):
        pn = pd.get('name')
        pt = pd.get('type')
        if not pn:
            raise SchemaError('pass missing name')
        if pt not in PASS_TYPES:
            raise SchemaError('pass %s: unknown type %r' % (pn, pt))
        binds = [_binding(b, 'pass %s' % pn) for b in (pd.get('bind') or [])]
        sample = [_binding(b, 'pass %s sample' % pn) for b in (pd.get('sample') or [])]
        depth = pd.get('depth')
        if depth is not None:
            if 'res' not in depth:
                raise SchemaError('pass %s: depth needs res' % pn)
            depth = {'res': depth['res'], 'access': depth.get('access', ACC_RW),
                     'store': depth.get('store', 'store')}
            if depth['access'] not in (ACC_READ, ACC_WRITE, ACC_RW):
                raise SchemaError('pass %s: bad depth access %r' % (pn, depth['access']))
            if depth['store'] not in ('store', 'dont_care'):
                raise SchemaError('pass %s: bad depth store %r (store|dont_care)'
                                  % (pn, depth['store']))
        copy = pd.get('copy')
        if copy is not None and ('src' not in copy or 'dst' not in copy):
            raise SchemaError('pass %s: copy needs src+dst' % pn)
        repeat = int(pd.get('repeat', 1))
        if repeat < 1:
            raise SchemaError('pass %s: repeat must be >=1' % pn)
        vertex = pd.get('vertex')
        if isinstance(vertex, str):          # allow a single-stream shorthand
            vertex = [vertex]
        passes.append(Pass(
            pn, pt, scope=pd.get('scope'), groups=int(pd.get('groups', 1)),
            binds=binds, color=pd.get('color'), depth=depth, sample=sample,
            vertex=vertex, index=pd.get('index'), copy=copy,
            swapchain=pd.get('swapchain'),
            repeat=repeat, marker=bool(pd.get('marker', True))))

    scene = Scene(name, resources, passes,
                  bundling=bool(data.get('bundling', False)),
                  frame_prior=bool(data.get('frame_prior', False)))
    _validate_refs(scene)
    return scene


def _validate_refs(scene):
    """Validate referenced resources."""
    def need(rn, where):
        if rn not in scene.resources:
            raise SchemaError('%s: undeclared resource %r' % (where, rn))
    for p in scene.passes:
        for b in p.binds + p.sample:
            need(b.res, 'pass %s' % p.name)
            kind = scene.resources[b.res].kind
            # cbuffer resources bind through cbv slots
            is_cbuf = kind == KIND_CBUFFER
            if b.bind == BIND_CBV and not is_cbuf:
                raise SchemaError('pass %s: cbv binding %s must reference a '
                                  'cbuffer resource' % (p.name, b.res))
            if is_cbuf and b.bind != BIND_CBV:
                raise SchemaError('pass %s: cbuffer resource %s can only be bound '
                                  'as cbv' % (p.name, b.res))
            # IA buffers are bound through vertex/index, never a shader slot.
            if kind in (KIND_VBUFFER, KIND_IBUFFER):
                raise SchemaError('pass %s: %s is an IA buffer, bind it via '
                                  'vertex/index not a shader slot' % (p.name, b.res))
        for c in p.color:
            need(c, 'pass %s color' % p.name)
        if p.depth:
            need(p.depth['res'], 'pass %s depth' % p.name)
        for v in p.vertex:
            need(v, 'pass %s vertex' % p.name)
            if scene.resources[v].kind != KIND_VBUFFER:
                raise SchemaError('pass %s: vertex %s must be a vbuffer resource'
                                  % (p.name, v))
        if p.index:
            need(p.index, 'pass %s index' % p.name)
            if scene.resources[p.index].kind != KIND_IBUFFER:
                raise SchemaError('pass %s: index %s must be an ibuffer resource'
                                  % (p.name, p.index))
        if p.copy:
            need(p.copy['src'], 'pass %s copy' % p.name)
            need(p.copy['dst'], 'pass %s copy' % p.name)
        if p.swapchain:
            need(p.swapchain, 'pass %s present' % p.name)


def load_scene(path):
    import yaml
    with open(path, 'r', encoding='utf-8') as f:
        return load_dict(yaml.safe_load(f))
