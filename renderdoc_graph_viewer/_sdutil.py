# -*- coding: utf-8 -*-
"""Structured-data child accessors shared by the zero-replay parsers
(descriptor_access shader refinement, graph_model depth adapters). RenderDoc
SDObject children are looked up by name and coerced defensively: a missing
child or a coercion failure yields the caller's default rather than raising,
so a single malformed chunk never aborts a whole-frame static walk."""

_NULL_RID = 'ResourceId::0'   # RenderDoc's null ResourceId stringifies to this


def _base(name):
    """Chunk/struct name without its API namespace prefix (the inverse of the
    '::'-tail match used to recognise chunk families)."""
    return name.split('::')[-1]


def _ci(o, name, default=0):
    """child int; default on missing child OR coercion failure (superset of the
    two former copies: guards both a None parent and a non-int child)."""
    c = o.FindChild(name) if o is not None else None
    try:
        return c.AsInt() if c is not None else default
    except Exception:
        return default


def _cstr(o, name):
    c = o.FindChild(name)
    try:
        return c.AsString() if c is not None else ''
    except Exception:
        return ''


def _crid_obj(o, name):
    """child ResourceId object (for APIs that need the object, e.g. GetShader)."""
    c = o.FindChild(name)
    try:
        return c.AsResourceId() if c is not None else None
    except Exception:
        return None


def _rid_str(rid):
    """ResourceId (object or already-stringified) as canonical str, or None for
    the null id / empty."""
    s = str(rid) if rid is not None else None
    return s if s and s != _NULL_RID else None


def _crid(o, name):
    """child ResourceId as canonical str (for keys / res_key comparison)."""
    return _rid_str(_crid_obj(o, name))


def _self_rid(el):
    try:
        return _rid_str(el.AsResourceId())
    except Exception:
        return None
