# -*- coding: utf-8 -*-
"""Structured-data child accessors shared by the static parsers."""

_NULL_RID = 'ResourceId::0'   # RenderDoc's null ResourceId stringifies to this


def _base(name):
    """Chunk/struct name without its API namespace prefix (the inverse of the
    '::'-tail match used to recognise chunk families)."""
    return name.split('::')[-1]


def _ci(o, name, default=0):
    """Child int, with default for missing or non-int values."""
    if o is None:
        return default
    c = o.FindChild(name)
    if c is None:
        return default
    try:
        return c.AsInt()
    except Exception:
        return default


def _cstr(o, name):
    """Child string, or '' for missing/non-string values."""
    c = o.FindChild(name)
    if c is None:
        return ''
    try:
        return c.AsString()
    except Exception:
        return ''


def _crid_obj(o, name):
    """Child ResourceId object."""
    c = o.FindChild(name)
    if c is None:
        return None
    try:
        return c.AsResourceId()
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
    """Element ResourceId as canonical str, or None."""
    try:
        return _rid_str(el.AsResourceId())
    except Exception:
        return None


def _last_resource_id(ch):
    """Last non-null ResourceId child."""
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
    """Chunk-name family match: bare name or namespaced '...::tail'."""
    return name == tail or name.endswith('::' + tail)
