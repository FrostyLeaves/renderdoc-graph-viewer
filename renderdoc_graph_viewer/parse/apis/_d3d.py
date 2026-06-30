# -*- coding: utf-8 -*-
"""Shared D3D11/D3D12 depth/stencil state classification.

This lives in the D3D family module instead of _common so _common stays
API-neutral. Do not import graph_model or shader_refinement here; keeping the
dependency one-way avoids import cycles.
"""

from .._sdutil import _ci as _child_int
from ._common import combine_depth_access

_D3D_COMPARISON_ALWAYS = 8     # D3D11/12_COMPARISON_FUNC_ALWAYS
_D3D_STENCIL_OP_KEEP = 1       # D3D11/12_STENCIL_OP_KEEP


def _d3d_depth_access(dss):
    """Classify a D3D depth/stencil desc into read/write/rw/none. D3D11,
    classic D3D12 and stream-PSO DESC2 share field names; only the stencil
    write mask differs (DESC2 per-face vs classic top-level), handled by the
    per-face fallback below."""
    if dss is None:
        return None            # unknown state: keep the conservative WRITE
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
    return combine_depth_access(reads, writes)
