# -*- coding: utf-8 -*-
"""Shared access vocabulary and per-API pass scaffolding."""

from .. import shader_access
from .. import action_flags

# Access-direction vocabulary. String values are persistent graph contracts.
READ = 'read'
WRITE = 'write'
RW = 'rw'
ACCESS_NONE = 'none'   # Depth: no active attachment; distinct from IGNORE.
UNUSED = 'unused'      # Shader binding declared but not accessed.


def _merge(cur, a):
    if cur is None or cur == a:
        return a
    if cur == UNUSED:   # UNUSED is weakest; any real access wins.
        return a
    if a == UNUSED:
        return cur
    return RW


def combine_depth_access(reads, writes):
    """Fold per-binding depth/stencil (reads, writes) flags into the access
    vocabulary. Shared by every API's depth classifier."""
    if writes:
        return RW if reads else WRITE
    return READ if reads else ACCESS_NONE


def _disasm_target(controller, enc):
    """Disassembly target string for an encoding: the explicit DXIL view when
    present, else the native default. SPIR-V never reaches here (it is binary)."""
    try:
        targets = controller.GetDisassemblyTargets(True)
    except Exception:
        return ''
    if not targets:
        return ''
    if enc == 'DXIL':
        for t in targets:
            if 'DXIL' in t:
                return t
    return targets[0]


def _reflect_verdicts(controller, rd, pipe_obj, shader_obj, entry, stage_name, cache):
    """GetShader reflection as [(space, binding, access)] for RW resources."""
    key = str(shader_obj)
    if key in cache:
        return cache[key]
    out = []
    try:
        st = getattr(rd.ShaderStage, stage_name, rd.ShaderStage.Compute)
        refl = controller.GetShader(pipe_obj, shader_obj,
                                    rd.ShaderEntryPoint(entry, st))
    except Exception:
        refl = None
    rwres = getattr(refl, 'readWriteResources', None) if refl else None
    if rwres:
        rwb = [{'index': i, 'bind': r.fixedBindNumber,
                'space': r.fixedBindSetOrSpace} for i, r in enumerate(rwres)]
        enc = str(refl.encoding).split('.')[-1]
        kind_fn = shader_access.PARSERS.get(enc)
        payload = None
        parsed = False
        verdicts = {}
        if kind_fn is not None:
            try:
                if kind_fn[0] == 'binary':
                    payload = bytes(refl.rawBytes)
                else:
                    payload = controller.DisassembleShader(
                        pipe_obj, refl, _disasm_target(controller, enc))
            except Exception:
                payload = None
        if payload:
            verdicts = shader_access.parse(enc, payload, rwb)
            parsed = True
        for i, r in enumerate(rwres):
            a = verdicts.get(i)
            if a in (READ, WRITE, RW):
                out.append((r.fixedBindSetOrSpace, r.fixedBindNumber, a))
            elif a is None and parsed:
                out.append((r.fixedBindSetOrSpace, r.fixedBindNumber, UNUSED))
            # a == UNKNOWN: touched but unattributable -> omit (conservative)
    cache[key] = out
    return out


def walk_actions(roots, on_action, chunks=None, on_chunk=None):
    """Recurse the action tree in event order. For each action: if on_chunk is
    given, feed every event's chunk to on_chunk(ch) (missing/out-of-range chunks
    skipped); then call on_action(action); then recurse into its children. The
    shared skeleton behind the shader-access, depth and label-cleanup walks."""
    def visit(acts):
        for a in acts:
            if on_chunk is not None:
                for ev in a.events:
                    try:
                        ch = chunks[ev.chunkIndex]
                    except Exception:
                        continue   # missing/out-of-range chunk for this event
                    on_chunk(ch)
            on_action(a)
            visit(a.children)
    visit(roots)


def _walk_executables(controller, rd, pass_cls, warnings=None):
    """Shared per-API refine skeleton. Instantiate pass_cls(controller, rd,
    chunks); walk the action tree feeding p.on_chunk(ch); on every executable
    action (Drawcall|Dispatch) fold p.attribute(a)'s (res_key, access) pairs
    into {(eid, res_key): merged}. Returns {} (with a warning) when SF / root
    actions are unavailable. _merge is order-independent, so visit order is
    free."""
    try:
        chunks = controller.GetStructuredFile().chunks
        roots = controller.GetRootActions()
    except Exception as exc:
        if warnings is not None:
            warnings.append('shader-access refinement: structured data '
                            'unavailable (%s); bindings keep conservative '
                            'read+write edges' % exc)
        return {}
    p = pass_cls(controller, rd, chunks)
    f_exec = action_flags.executable(rd)
    result = {}

    def on_action(a):
        if a.flags & f_exec:
            for (res, acc) in p.attribute(a):
                k = (a.eventId, res)
                result[k] = _merge(result.get(k), acc)

    walk_actions(roots, on_action, chunks, p.on_chunk)
    return result
