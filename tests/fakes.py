# -*- coding: utf-8 -*-
"""Helpers to build LeafAction IR concisely in tests, plus fake renderdoc
module/action objects for exercising _collect_leaves without RenderDoc."""

from renderdoc_graph_viewer.graph_model import LeafAction


class FakeRD(object):
    """Stands in for the renderdoc module in _collect_leaves(rd, ...)."""

    class ActionFlags(object):
        Drawcall = 1
        Dispatch = 2
        Clear = 4
        Copy = 8
        Resolve = 16
        GenMips = 32
        Present = 64
        PushMarker = 128
        PopMarker = 256
        SetMarker = 512
        MultiAction = 1024
        CmdList = 2048
        BeginPass = 4096
        EndPass = 8192
        PassBoundary = 16384
        MeshDispatch = 32768
        DispatchRay = 65536


class FakeAction(object):
    def __init__(self, eid, flags, name='', children=(), outputs=(),
                 depthOut=None, copySource=None, copyDestination=None):
        self.eventId = eid
        self.flags = flags
        self.customName = name
        self.children = list(children)
        self.outputs = list(outputs)
        self.depthOut = depthOut
        self.copySource = copySource
        self.copyDestination = copyDestination

    def GetName(self, sdfile):
        return self.customName


def draw(eid, outputs=(), depth=None, markers=(), name=''):
    return LeafAction(eid, 'draw', group_outputs=outputs, group_depth=depth,
                      marker_path=markers, name=name)


def dispatch(eid, markers=(), name=''):
    return LeafAction(eid, 'dispatch', marker_path=markers, name=name)


def clear(eid, outputs=(), depth=None, markers=(), name='Clear'):
    return LeafAction(eid, 'clear', group_outputs=outputs, group_depth=depth,
                      marker_path=markers, name=name)


def transfer(eid, src=None, dst=None, markers=(), name='Copy'):
    return LeafAction(eid, 'transfer', marker_path=markers, name=name,
                      copy_src_hint=src, copy_dst_hint=dst)


def present(eid, src=None, name='Present'):
    return LeafAction(eid, 'present', copy_src_hint=src, name=name)


# Compute-bundle helpers for shader_access tests.
CS_EID = 10  # event-id of the single compute dispatch in compute_bundle()


def compute_bundle(writes_rw=()):
    """Minimal bundle with one compute dispatch using CS_RWResource."""
    res_keys = list(writes_rw)
    res_info = {k: {'kind': 'buffer', 'info': {}} for k in res_keys}
    res_names = {k: k for k in res_keys}
    leaves = [dispatch(CS_EID, markers=('Frame', 'CS'), name='Compute')]
    usage_by_res = {k: [(CS_EID, 'CS_RWResource')] for k in res_keys}
    return {
        'leaves': leaves,
        'usage_by_res': usage_by_res,
        'res_info': res_info,
        'res_names': res_names,
        'rid_objects': {},
        'warnings': [],
        'seconds': 0.0,
    }
