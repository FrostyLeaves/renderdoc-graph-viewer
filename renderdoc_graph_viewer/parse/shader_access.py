# -*- coding: utf-8 -*-
"""Static shader-bytecode access classification: for each read-write binding,
decide read-only / write-only / read-write, or UNKNOWN when it cannot be
attributed statically. Pure functions, no renderdoc import, unit-testable
offline. Dispatched by ShaderEncoding; per-API refinement passes prepare the
payload (disassembly text or raw bytes) per PARSERS' input kind."""

import re

READ = 'read'
WRITE = 'write'
RW = 'rw'
UNKNOWN = 'unknown'   # touched but not statically attributable -> caller degrades


def _merge(cur, a):
    """Combine two access verdicts on the same binding."""
    if cur is None or cur == a:
        return a
    if UNKNOWN in (cur, a):
        return UNKNOWN
    return RW   # read + write


def _all_unknown(rw_bindings):
    # caller contract: each binding is a well-formed dict carrying 'index'
    return dict((b['index'], UNKNOWN) for b in rw_bindings)


# encoding -> (input_kind, parser). input_kind: 'disasm' text | 'binary' bytes.
# Populated as parsers land (later tasks); unknown encodings dispatch to nothing.
PARSERS = {}


_DXBC_UREG = re.compile(r'\bu(\d+)\b')
# RenderDoc disassembly declares each UAV with its name and register, e.g.
# "dcl_uav_structured g_Work (u0), 4" or "dcl_uav_typed_texture2d ... p_Img (u3)".
_DXBC_DCL_UAV = re.compile(r'\bdcl_uav_\w+\b[^\n]*?\b([A-Za-z_]\w*)\s*\(u(\d+)\)')
_DXBC_READ = ('ld_uav', 'ld_structured', 'ld_raw')
_DXBC_WRITE = ('store_uav', 'store_structured', 'store_raw')


def _parse_dxbc(text, rw_bindings):
    by_bind = dict((b['bind'], b['index']) for b in rw_bindings)
    # RenderDoc names instruction operands (g_Work, p_Accum, ...); map each
    # declared UAV name to its u register so we can attribute named operands.
    name_bind = dict((m.group(1), int(m.group(2)))
                     for m in _DXBC_DCL_UAV.finditer(text))
    acc = {}
    for raw in text.splitlines():
        s = raw.strip()
        if not s or s.startswith('//'):
            continue
        m = re.match(r'^\d+:\s*(.*)', s)   # drop the "N:" line-number prefix
        if m:
            s = m.group(1)
        op = re.split(r'[ (]', s, 1)[0]
        if op.startswith('dcl_'):
            continue
        is_read = op.startswith(_DXBC_READ)
        is_write = op.startswith(_DXBC_WRITE)
        is_atomic = op.startswith('atomic_') or op.startswith('imm_atomic_')
        if not (is_read or is_write or is_atomic):
            continue
        if 'u[' in s:   # dynamic-indexed UAV array: not statically attributable
            return _all_unknown(rw_bindings)
        a = RW if is_atomic else (READ if is_read else WRITE)
        hit = False
        for name, bind in name_bind.items():     # named operands (RenderDoc)
            if re.search(r'\b' + re.escape(name) + r'\b', s):
                idx = by_bind.get(bind)
                if idx is not None:
                    acc[idx] = _merge(acc.get(idx), a)
                hit = True
        if not hit:                              # bare uN operands (no debug names)
            for slot in _DXBC_UREG.findall(s):
                idx = by_bind.get(int(slot))
                if idx is not None:
                    acc[idx] = _merge(acc.get(idx), a)
    return acc


PARSERS['DXBC'] = ('disasm', _parse_dxbc)


import struct as _struct

_SPV_MAGIC = 0x07230203
_SPV = dict(Name=5, EntryPoint=15, TypeImage=25, TypePointer=32, Variable=59,
            Load=61, Store=62, AccessChain=65, InBoundsAccessChain=66,
            PtrAccessChain=67, CopyObject=83, ImageTexelPointer=60, ImageRead=98,
            ImageWrite=99, Decorate=71, Function=54, FunctionParameter=55,
            FunctionEnd=56, FunctionCall=57)
_SPV_DEC_BINDING = 33
_SPV_DEC_SET = 34
_SPV_ATOMIC = set(range(227, 243))   # OpAtomic* core range
_SPV_ATOMIC_LOAD = 227
_SPV_ATOMIC_STORE = 228


def _parse_spirv(blob, rw_bindings):
    n = len(blob) // 4
    if n < 5:
        return {}
    words = _struct.unpack('<%dI' % n, blob[:n * 4])
    if words[0] != _SPV_MAGIC:
        return _all_unknown(rw_bindings)   # endianness/format unexpected
    by_sb = dict(((b['space'], b['bind']), b['index']) for b in rw_bindings)

    instrs = []
    i = 5
    while i < n:
        w = words[i]
        op, wc = w & 0xFFFF, w >> 16
        if wc == 0:
            break
        instrs.append((op, words[i + 1:i + wc]))
        i += wc

    # pass 1: module-scope decorations, types, variables
    dset, dbind, ptr_pointee, image_types, var_type = {}, {}, {}, set(), {}
    for op, ops in instrs:
        if op == _SPV['Decorate'] and len(ops) >= 2:
            if ops[1] == _SPV_DEC_SET and len(ops) >= 3:
                dset[ops[0]] = ops[2]
            elif ops[1] == _SPV_DEC_BINDING and len(ops) >= 3:
                dbind[ops[0]] = ops[2]
        elif op == _SPV['TypeImage'] and ops:
            image_types.add(ops[0])
        elif op == _SPV['TypePointer'] and len(ops) >= 3:
            ptr_pointee[ops[0]] = ops[2]
        elif op == _SPV['Variable'] and len(ops) >= 2:
            var_type[ops[1]] = ops[0]

    rw_var, var_is_image = {}, {}
    for vid, ptype in var_type.items():
        idx = by_sb.get((dset.get(vid, 0), dbind.get(vid)))
        if idx is None:
            continue
        rw_var[vid] = idx
        var_is_image[vid] = ptr_pointee.get(ptype) in image_types
    if not rw_var:
        return {}

    # pass 1.5: split the body into functions. Module-scope instructions land
    # under key None; each OpFunction gathers its parameter ids and its body.
    func_params, func_body, entry_funcs = {}, {None: []}, []
    cur = None
    for op, ops in instrs:
        if op == _SPV['EntryPoint'] and len(ops) >= 2:
            entry_funcs.append(ops[1])
        elif op == _SPV['Function'] and len(ops) >= 2:
            cur = ops[1]
            func_params[cur] = []
            func_body[cur] = []
        elif op == _SPV['FunctionParameter'] and len(ops) >= 2:
            if cur is not None:
                func_params[cur].append(ops[1])
        elif op == _SPV['FunctionEnd']:
            cur = None
        else:
            func_body[cur].append((op, ops))

    # pass 2: evaluate from each entry point, binding a callee's parameters to
    # the roots of its call-site arguments so pointer flow crosses calls. Each
    # call site evaluates the callee with its own bindings, so one helper reused
    # with different resources stays correctly attributed.
    acc = {}

    def mark(vid, a):
        if vid in rw_var:
            acc[rw_var[vid]] = _merge(acc.get(rw_var[vid]), a)

    def eval_func(fid, param_root, stack):
        body = func_body.get(fid)
        if body is None or fid in stack:
            return
        stack = stack | {fid}
        ptr_root = dict(param_root)   # local SSA id -> pointer root, seeded by params
        img_handle = {}

        def root(pid):
            seen = 0
            while pid in ptr_root and seen < 4096:
                pid = ptr_root[pid]
                seen += 1
            return pid

        for op, ops in body:
            if op in (_SPV['AccessChain'], _SPV['InBoundsAccessChain'],
                      _SPV['PtrAccessChain'], _SPV['ImageTexelPointer']) and len(ops) >= 3:
                ptr_root[ops[1]] = root(ops[2])
            elif op == _SPV['CopyObject'] and len(ops) >= 3:
                ptr_root[ops[1]] = root(ops[2])
            elif op == _SPV['Load'] and len(ops) >= 3:
                r = root(ops[2])
                if r in rw_var:
                    if var_is_image.get(r):
                        img_handle[ops[1]] = r      # image handle load, not data
                    else:
                        mark(r, READ)
            elif op == _SPV['Store'] and len(ops) >= 1:
                r = root(ops[0])
                if not var_is_image.get(r):
                    mark(r, WRITE)
            elif op == _SPV['ImageWrite'] and ops:
                mark(img_handle.get(ops[0]), WRITE)
            elif op == _SPV['ImageRead'] and len(ops) >= 3:
                mark(img_handle.get(ops[2]), READ)
            elif op in _SPV_ATOMIC:
                ptr = ops[0] if op == _SPV_ATOMIC_STORE else (
                    ops[2] if len(ops) >= 3 else None)
                if ptr is not None:
                    mark(root(ptr), READ if op == _SPV_ATOMIC_LOAD else RW)
            elif op == _SPV['FunctionCall'] and len(ops) >= 3:
                callee, args = ops[2], ops[3:]
                if callee in func_body:
                    child = {}
                    for k, pid in enumerate(func_params.get(callee, [])):
                        if k < len(args):
                            child[pid] = root(args[k])
                    eval_func(callee, child, stack)
                else:
                    # callee body absent (external/linked): a passed RW pointer
                    # could be used any way -> UNKNOWN, never a silent NONE
                    for arg in args:
                        mark(root(arg), UNKNOWN)

    starts = [f for f in entry_funcs if f in func_body]
    if not starts:
        starts = [k for k in func_body if k is not None] or [None]
    for f in starts:
        eval_func(f, {}, frozenset())
    return acc


PARSERS['SPIRV'] = ('binary', _parse_spirv)


# RenderDoc disassembles DXIL to high-level pseudo-code, not raw LLVM IR:
#   RWStructuredBuffer<int> g_Work : register(u0, space0);     -- UAV decl
#   Handle _10 = InitialiseHandle(g_Work); //  index = 0       -- handle -> resource
#   _11.Load(...)         read       _10.Store(...)       write
#   _17[x, y] = {...}     image write (subscript on lhs)
#   _10.InterlockedAdd(...)          atomic -> read-write
_DXIL_DECL = re.compile(
    r'\b([A-Za-z_]\w*)\s*:\s*register\(\s*u(\d+)\s*,\s*space(\d+)\s*\)')
_DXIL_INIT = re.compile(r'Handle\s+([_\w]+)\s*=\s*InitialiseHandle\((\w+)\)')
# Typed-cast handle alias.
_DXIL_CAST = re.compile(r'\b([_\w]+)\s*=\s*\([^()]*\)\s*([_\w]+)\b')
_DXIL_METHOD = re.compile(r'\b([_\w]+)\.([A-Za-z]\w*)\s*\(')
_DXIL_SUBSCRIPT_W = re.compile(r'\b([_\w]+)\[[^\]]*\]\s*=')
_DXIL_SUBSCRIPT_R = re.compile(r'=\s*([_\w]+)\[[^\]]*\]')


def _parse_dxil(text, rw_bindings):
    # raw LLVM-IR form (no high-level decode) carries no attributable binding
    if 'InitialiseHandle' not in text and '@dx.op.' in text:
        return _all_unknown(rw_bindings)
    by_sb = dict(((b['space'], b['bind']), b['index']) for b in rw_bindings)
    name_idx = {}                       # UAV resource name -> rw index
    for m in _DXIL_DECL.finditer(text):
        idx = by_sb.get((int(m.group(3)), int(m.group(2))))
        if idx is not None:
            name_idx[m.group(1)] = idx
    handle_idx = {}                     # handle var -> rw index
    for m in _DXIL_INIT.finditer(text):
        if m.group(2) in name_idx:
            handle_idx[m.group(1)] = name_idx[m.group(2)]
    # propagate bindings across typed-cast handle aliases (fixpoint for chains)
    casts = [(m.group(1), m.group(2)) for m in _DXIL_CAST.finditer(text)]
    for _ in range(8):
        grew = False
        for newv, src in casts:
            if src in handle_idx and newv not in handle_idx:
                handle_idx[newv] = handle_idx[src]
                grew = True
        if not grew:
            break
    acc = {}

    def hit(idx, a):
        if idx is not None:
            acc[idx] = _merge(acc.get(idx), a)

    for line in text.splitlines():
        for m in _DXIL_METHOD.finditer(line):
            idx = handle_idx.get(m.group(1))
            if idx is None:
                continue
            meth = m.group(2)
            if meth == 'Load':
                hit(idx, READ)
            elif meth == 'Store':
                hit(idx, WRITE)
            elif meth.startswith('Interlocked') or meth.startswith('Atomic'):
                hit(idx, RW)
        for m in _DXIL_SUBSCRIPT_W.finditer(line):   # image write: handle[..] =
            hit(handle_idx.get(m.group(1)), WRITE)
        for m in _DXIL_SUBSCRIPT_R.finditer(line):   # image read:  = handle[..]
            hit(handle_idx.get(m.group(1)), READ)
    return acc


PARSERS['DXIL'] = ('disasm', _parse_dxil)


def parse(encoding, payload, rw_bindings):
    """Classify each read-write binding -> {rw_index: READ|WRITE|RW|UNKNOWN}.
    Unsupported encoding / empty payload / no bindings -> {}."""
    entry = PARSERS.get(encoding)
    if entry is None or not payload or not rw_bindings:
        return {}
    _input_kind, fn = entry
    try:
        return fn(payload, rw_bindings)
    except Exception:
        return _all_unknown(rw_bindings)
