# -*- coding: utf-8 -*-
"""Canonical per-pass binding layout shared by codegen, manifest and (via the
manifest) the three runtimes, so every API agrees on which resource sits at which
HLSL register / Vulkan binding. Python 3.6."""

from . import schema as S

# HLSL register class per binding kind
_REG_CLASS = {
    S.BIND_SRV_BUF: 't',
    S.BIND_SAMPLED: 't',
    S.BIND_CBV: 'b',
    S.BIND_UAV_BUF: 'u',
    S.BIND_UAV_TEX: 'u',
}

# Vulkan descriptor type name per binding kind (consumed by generic_vk.cpp)
VK_DTYPE = {
    S.BIND_SRV_BUF: 'storage_buffer',
    S.BIND_UAV_BUF: 'storage_buffer',
    S.BIND_UAV_TEX: 'storage_image',
    S.BIND_SAMPLED: 'sampled_image',
    S.BIND_CBV: 'uniform_buffer',
}


def assign(binds):
    """binds: list[schema.Binding] -> list[dict] with stable slot assignment.

    vk_binding is a single sequential index per pass (set 0). reg_class/reg_index
    give the D3D register (t#/u#/b#) numbered per class in declaration order."""
    counters = {'t': 0, 'u': 0, 'b': 0}
    out = []
    for i, b in enumerate(binds):
        cls = _REG_CLASS[b.bind]
        idx = counters[cls]
        counters[cls] += 1
        out.append({
            'res': b.res,
            'bind': b.bind,
            'access': b.access,
            'atomic': getattr(b, 'atomic', False),
            'ident': 'g%d' % i,            # HLSL identifier
            'vk_binding': i,               # Vulkan set-0 binding number
            'reg_class': cls,              # 't' | 'u' | 'b'
            'reg_index': idx,              # per-class register number
            'hlsl_reg': '%s%d' % (cls, idx),
            'vk_dtype': VK_DTYPE[b.bind],
        })
    return out
