# -*- coding: utf-8 -*-
"""Shared fake 'renderdoc' module for unit tests."""

import sys
import types


class ResourceId(object):
    def __init__(self, s):
        self.s = s

    @staticmethod
    def Null():
        return _NULL

    def __eq__(self, other):
        return isinstance(other, ResourceId) and self.s == other.s

    def __ne__(self, other):
        return not self.__eq__(other)

    def __hash__(self):
        return hash(self.s)

    def __str__(self):
        return self.s


_NULL = ResourceId('ResourceId::0')


class TextureSave(object):
    def __init__(self):
        self.resourceId = None
        self.mip = 0
        self.slice = types.SimpleNamespace(sliceIndex=0)
        self.alpha = None
        self.destType = None
        self.comp = types.SimpleNamespace(blackPoint=0.0, whitePoint=1.0)


class DescriptorCategory(object):
    ReadOnly = 'ReadOnly'
    ReadWrite = 'ReadWrite'
    ConstantBlock = 'ConstantBlock'
    Sampler = 'Sampler'


def CategoryForDescriptorType(t):
    # tests put a DescriptorCategory value directly in descriptor.type; echo it
    return t


def install():
    mod = sys.modules.get('renderdoc')
    if mod is not None and getattr(mod, '_is_test_stub', False):
        return mod
    mod = types.ModuleType('renderdoc')
    mod._is_test_stub = True
    mod.ResourceId = ResourceId
    mod.TextureSave = TextureSave
    mod.Subresource = lambda mip=0, slice=0, sample=0: types.SimpleNamespace(
        mip=mip, slice=slice, sample=sample)
    mod.CompType = types.SimpleNamespace(
        Typeless='typeless', Float='float', UNorm='unorm', SNorm='snorm')
    mod.AlphaMapping = types.SimpleNamespace(BlendToCheckerboard=1)
    # PNG only: qrenderdoc's bundled Qt has no JPG decode plugin -
    # omitting JPG makes an accidental revert fail loudly
    mod.FileType = types.SimpleNamespace(PNG=3)
    # creation-flag categories: values mirror renderdoc_replay.h
    mod.TextureCategory = types.SimpleNamespace(
        NoFlags=0x0, ShaderRead=0x1, ColorTarget=0x2, DepthTarget=0x4,
        ShaderReadWrite=0x8, SwapBuffer=0x10)
    mod.BufferCategory = types.SimpleNamespace(
        NoFlags=0x0, Vertex=0x1, Index=0x2, Constants=0x4, ReadWrite=0x8,
        Indirect=0x10)
    # extract_bundle/_collect_leaves getattr() member names with a
    # default - missing names resolve to 0. Marker-class flags carry
    # real renderdoc values for the label-detection tests.
    mod.ResourceUsage = types.SimpleNamespace()
    mod.ActionFlags = types.SimpleNamespace(
        SetMarker=0x20, PushMarker=0x40, PopMarker=0x80,
        CmdList=0x10000)
    # depth-access refinement
    mod.GraphicsAPI = types.SimpleNamespace(
        Vulkan='vulkan', D3D11='d3d11', D3D12='d3d12', OpenGL='opengl')
    mod.CompareFunction = types.SimpleNamespace(
        AlwaysTrue='always', Never='never', Less='less', LessEqual='lequal')
    mod.StencilOperation = types.SimpleNamespace(
        Keep='keep', Replace='replace', Zero='zero')
    # shader-access refinement (shader_refinement four-state verdicts)
    mod.DescriptorCategory = DescriptorCategory
    mod.CategoryForDescriptorType = CategoryForDescriptorType
    sys.modules['renderdoc'] = mod
    return mod
