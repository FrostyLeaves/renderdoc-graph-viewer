# -*- coding: utf-8 -*-
"""Shared RenderDoc ActionFlags groupings.

RenderDoc adds action categories over time. Keeping the combinations here
prevents graph extraction, usage cleanup and shader refinement from drifting.
"""


def flag(rd, name):
    return getattr(rd.ActionFlags, name, 0)


def draw(rd):
    return flag(rd, 'Drawcall') | flag(rd, 'MeshDispatch')


def dispatch(rd):
    return flag(rd, 'Dispatch') | flag(rd, 'DispatchRay')


def executable(rd):
    return draw(rd) | dispatch(rd)


def transfer(rd):
    return flag(rd, 'Copy') | flag(rd, 'Resolve') | flag(rd, 'GenMips')


def structural(rd):
    return (flag(rd, 'CmdList') | flag(rd, 'PassBoundary') |
            flag(rd, 'BeginPass') | flag(rd, 'EndPass') |
            flag(rd, 'CommandBufferBoundary'))


def marker(rd):
    return flag(rd, 'PushMarker') | flag(rd, 'PopMarker') | flag(rd, 'SetMarker')
