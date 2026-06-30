# -*- coding: utf-8 -*-
"""Static shader-bytecode access classification (parse.shader_access): per
read-write binding decide read / write / rw / UNKNOWN, dispatched by encoding
(DXBC, SPIR-V, DXIL). Payloads here are hand-built disassembly text / minimal
SPIR-V word streams; real compiled bytecode is covered by the e2e suite."""
import struct
import unittest

from renderdoc_graph_viewer.parse import shader_access as sa


class TestConstantsAndMerge(unittest.TestCase):
    def test_constants_match_graph_model_values(self):
        # 必须与 graph_model 的 READ/WRITE/RW 字符串值一致(应用层按值比较)
        self.assertEqual((sa.READ, sa.WRITE, sa.RW), ('read', 'write', 'rw'))

    def test_merge_combines_directions(self):
        self.assertEqual(sa._merge(None, sa.READ), sa.READ)
        self.assertEqual(sa._merge(sa.READ, sa.READ), sa.READ)
        self.assertEqual(sa._merge(sa.READ, sa.WRITE), sa.RW)
        self.assertEqual(sa._merge(sa.WRITE, sa.READ), sa.RW)
        self.assertEqual(sa._merge(sa.RW, sa.READ), sa.RW)
        self.assertEqual(sa._merge(sa.READ, sa.UNKNOWN), sa.UNKNOWN)


class TestParseGuards(unittest.TestCase):
    def test_unsupported_encoding_returns_empty(self):
        # known-bad encoding with a real binding -> PARSERS lookup misses -> {}
        self.assertEqual(
            sa.parse('GLSL', 'whatever', [{'index': 0, 'bind': 0, 'space': 0}]),
            {})

    def test_empty_payload_or_no_bindings_returns_empty(self):
        # both early-exit guards in parse(); no real parser exists yet at this stage
        self.assertEqual(
            sa.parse('SPIRV', b'', [{'index': 0, 'bind': 0, 'space': 0}]), {})
        self.assertEqual(sa.parse('DXBC', 'whatever', []), {})


_DXBC_RW = [  # reflection.readWriteResources 投影:bind == u 寄存器号
    {'index': 0, 'bind': 0, 'space': 0},
    {'index': 1, 'bind': 1, 'space': 0},
    {'index': 2, 'bind': 2, 'space': 0},
]


class TestDXBC(unittest.TestCase):
    def test_read_write_rw(self):
        text = '\n'.join([
            'dcl_uav_typed_buffer (float,float,float,float) u0',
            'dcl_uav_structured u1, 4',
            'dcl_uav_raw u2',
            'ld_uav_typed r0.x, l(0), u0.xxxx',      # u0 read-only
            'store_structured u1, r0.x, l(0), r0.y',  # u1 write-only
            'ld_raw r1.x, r0.x, u2',                  # u2 read...
            'store_raw u2, r0.x, r1.x',               # ...and write -> rw
        ])
        self.assertEqual(sa.parse('DXBC', text, _DXBC_RW),
                         {0: sa.READ, 1: sa.WRITE, 2: sa.RW})

    def test_atomic_is_rw(self):
        text = 'imm_atomic_iadd r0.x, u0, r0.y, r0.z'
        self.assertEqual(sa.parse('DXBC', text, _DXBC_RW)[0], sa.RW)

    def test_untouched_binding_absent(self):
        text = 'store_raw u1, r0.x, r1.x'
        out = sa.parse('DXBC', text, _DXBC_RW)
        self.assertEqual(out, {1: sa.WRITE})          # u0/u2 没出现 = NONE

    def test_dynamic_indexed_array_is_unknown(self):
        text = 'store_raw u[r0.x + 0], r0.y, r0.z'
        out = sa.parse('DXBC', text, _DXBC_RW)
        self.assertEqual(set(out.values()), {sa.UNKNOWN})   # 整体降级

    def test_named_operands_renderdoc(self):
        # RenderDoc disassembly: named operands + "N:" line prefixes (not bare uN)
        text = '\n'.join([
            '      dcl_uav_structured g_Work (u0), 4',
            '      dcl_uav_structured g_Accum (u1), 4',
            '   0: ld_structured_indexable(structured_buffer, stride=4)(mixed) '
            'r0.x, vThreadID.x, l(0), g_Work.xxxx',          # g_Work read
            '   1: store_structured g_Accum.x, vThreadID.x, l(0), r0.x',  # g_Accum write
            '   2: store_structured g_Work.x, vThreadID.x, l(0), r0.x',   # g_Work write -> rw
            '   3: ret',
        ])
        rw = [{'index': 0, 'bind': 0, 'space': 0},
              {'index': 1, 'bind': 1, 'space': 0}]
        self.assertEqual(sa.parse('DXBC', text, rw), {0: sa.RW, 1: sa.WRITE})

    def test_named_atomic_renderdoc(self):
        text = '\n'.join([
            '      dcl_uav_structured r_Result (u2), 4',
            '   0: atomic_iadd r_Result, l(0, 0, 0, 0), r0.x',   # atomic -> rw
            '   1: ret',
        ])
        self.assertEqual(
            sa.parse('DXBC', text, [{'index': 0, 'bind': 2, 'space': 0}]),
            {0: sa.RW})


# 最小 SPIR-V 字节流构造器:只放解析器关心的指令
_OP = dict(Decorate=71, TypeImage=25, TypePointer=32, Variable=59,
           Load=61, Store=62, AccessChain=65, ImageWrite=99, ImageRead=98,
           EntryPoint=15, Function=54, FunctionParameter=55, FunctionEnd=56,
           FunctionCall=57)


def _spirv(words_body):
    header = [0x07230203, 0x00010000, 0, 100, 0]   # magic, version, gen, bound, schema
    return struct.pack('<%dI' % (len(header) + len(words_body)),
                       *(header + words_body))


def _ins(op, operands):
    return [((len(operands) + 1) << 16) | op] + list(operands)


_RW5 = [{'index': 0, 'bind': 5, 'space': 0}]


def _spirv_with_helper(helper_body):
    # main(70) calls helper(80) passing storage-buffer var(11) as its one arg;
    # helper operates on parameter 90. Exercises inter-procedural tracking.
    body = []
    body += _ins(_OP['TypePointer'], [10, 12, 99])
    body += _ins(_OP['Variable'], [10, 11, 12])
    body += _ins(_OP['Decorate'], [11, 34, 0])
    body += _ins(_OP['Decorate'], [11, 33, 5])
    body += _ins(_OP['EntryPoint'], [5, 70])
    body += _ins(_OP['Function'], [200, 70, 0, 201])
    body += _ins(_OP['FunctionCall'], [99, 60, 80, 11])     # helper(var 11)
    body += _ins(_OP['FunctionEnd'], [])
    body += _ins(_OP['Function'], [200, 80, 0, 202])
    body += _ins(_OP['FunctionParameter'], [10, 90])        # param 90 = the buffer
    body += helper_body
    body += _ins(_OP['FunctionEnd'], [])
    return _spirv(body)


class TestSPIRV(unittest.TestCase):
    # ids: 10 ptrType(set0/bind5) 11 var(buffer) 12 chain 13 loadedVal ...
    def test_ssbo_read_only(self):
        body = []
        body += _ins(_OP['TypePointer'], [10, 12, 99])    # 10 = ptr, sc=StorageBuffer(12), pointee 99(non-image)
        body += _ins(_OP['Variable'], [10, 11, 12])       # 11 = var of type 10
        body += _ins(_OP['Decorate'], [11, 34, 0])        # set 0
        body += _ins(_OP['Decorate'], [11, 33, 5])        # binding 5
        body += _ins(_OP['AccessChain'], [10, 20, 11, 0])  # 20 = &var[..]
        body += _ins(_OP['Load'], [99, 21, 20])           # load from 20 -> READ
        rw = [{'index': 0, 'bind': 5, 'space': 0}]
        self.assertEqual(sa.parse('SPIRV', _spirv(body), rw), {0: sa.READ})

    def test_ssbo_write_then_read_is_rw(self):
        body = []
        body += _ins(_OP['TypePointer'], [10, 12, 99])
        body += _ins(_OP['Variable'], [10, 11, 12])
        body += _ins(_OP['Decorate'], [11, 34, 0])
        body += _ins(_OP['Decorate'], [11, 33, 5])
        body += _ins(_OP['AccessChain'], [10, 20, 11, 0])
        body += _ins(_OP['Store'], [20, 50])              # WRITE
        body += _ins(_OP['Load'], [99, 21, 20])           # READ
        rw = [{'index': 0, 'bind': 5, 'space': 0}]
        self.assertEqual(sa.parse('SPIRV', _spirv(body), rw), {0: sa.RW})

    def test_storage_image_write_only(self):
        body = []
        body += _ins(_OP['TypeImage'], [98])              # 98 = image type
        body += _ins(_OP['TypePointer'], [10, 0, 98])     # ptr to image, sc=UniformConstant(0)
        body += _ins(_OP['Variable'], [10, 11, 0])
        body += _ins(_OP['Decorate'], [11, 34, 0])
        body += _ins(_OP['Decorate'], [11, 33, 7])
        body += _ins(_OP['Load'], [98, 30, 11])           # load image handle (not a data read)
        body += _ins(_OP['ImageWrite'], [30, 40, 50])     # write via handle -> WRITE
        rw = [{'index': 0, 'bind': 7, 'space': 0}]
        self.assertEqual(sa.parse('SPIRV', _spirv(body), rw), {0: sa.WRITE})

    def test_function_call_tracks_param_write(self):
        # helper stores through the parameter -> WRITE attributed to the buffer
        helper = (_ins(_OP['AccessChain'], [10, 91, 90, 0]) +
                  _ins(_OP['Store'], [91, 500]))
        self.assertEqual(
            sa.parse('SPIRV', _spirv_with_helper(helper), _RW5), {0: sa.WRITE})

    def test_function_call_tracks_param_read(self):
        # helper loads through the parameter -> READ attributed to the buffer
        helper = (_ins(_OP['AccessChain'], [10, 91, 90, 0]) +
                  _ins(_OP['Load'], [99, 92, 91]))
        self.assertEqual(
            sa.parse('SPIRV', _spirv_with_helper(helper), _RW5), {0: sa.READ})

    def test_unknown_callee_degrades(self):
        # call into a function with no body in this module (external/import): the
        # passed RW pointer could be used arbitrarily -> UNKNOWN, never silent NONE
        body = []
        body += _ins(_OP['TypePointer'], [10, 12, 99])
        body += _ins(_OP['Variable'], [10, 11, 12])
        body += _ins(_OP['Decorate'], [11, 34, 0])
        body += _ins(_OP['Decorate'], [11, 33, 5])
        body += _ins(_OP['FunctionCall'], [99, 60, 70, 11])     # callee 70 undefined here
        self.assertEqual(sa.parse('SPIRV', _spirv(body), _RW5), {0: sa.UNKNOWN})


_DXIL_RW = [{'index': 0, 'bind': 0, 'space': 0},
            {'index': 1, 'bind': 1, 'space': 0}]


class TestDXIL(unittest.TestCase):
    def test_renderdoc_read_write(self):
        # RenderDoc high-level DXIL: register decls + InitialiseHandle + Load/Store
        text = '\n'.join([
            'RWStructuredBuffer<int> g_A : register(u0, space0);',
            'RWStructuredBuffer<int> g_B : register(u1, space0);',
            '  _dx.types.Handle _10 = InitialiseHandle(g_A); //  index = 0',
            '  _dx.types.Handle _11 = InitialiseHandle(g_B); //  index = 1',
            '  _dx.types.ResRet.i32 _13 = _10.Load(_12);',            # g_A read
            '  _11.Store(_12, byteOffset = 0, {_15});',                # g_B write
        ])
        self.assertEqual(sa.parse('DXIL', text, _DXIL_RW),
                         {0: sa.READ, 1: sa.WRITE})

    def test_renderdoc_load_then_store_is_rw(self):
        text = '\n'.join([
            'RWStructuredBuffer<int> g_A : register(u0, space0);',
            '  _dx.types.Handle _10 = InitialiseHandle(g_A); //  index = 0',
            '  _dx.types.ResRet.i32 _13 = _10.Load(_12);',
            '  _10.Store(_12, byteOffset = 0, {_15});',
        ])
        self.assertEqual(
            sa.parse('DXIL', text, [{'index': 0, 'bind': 0, 'space': 0}]),
            {0: sa.RW})

    def test_renderdoc_atomic_is_rw(self):
        text = '\n'.join([
            'RWStructuredBuffer<int> g_R : register(u0, space0);',
            '  _dx.types.Handle _10 = InitialiseHandle(g_R); //  index = 0',
            '  int _19 = _10.InterlockedAdd({0, 0}, _18);',
        ])
        self.assertEqual(
            sa.parse('DXIL', text, [{'index': 0, 'bind': 0, 'space': 0}]),
            {0: sa.RW})

    def test_renderdoc_image_write(self):
        text = '\n'.join([
            'RWTexture2D<float4> g_Img : register(u0, space0);',
            '  _dx.types.Handle _17 = InitialiseHandle(g_Img); //  index = 0',
            '  _17[_29, _30] = {_28, 0.000000, 0.000000, 1.00000};',   # image write subscript
        ])
        self.assertEqual(
            sa.parse('DXIL', text, [{'index': 0, 'bind': 0, 'space': 0}]),
            {0: sa.WRITE})

    def test_legacy_llvm_ir_degrades(self):
        # raw LLVM IR without RenderDoc's high-level decode -> conservative UNKNOWN
        text = ('%h = call %dx.types.Handle @dx.op.createHandle('
                'i32 57, i8 1, i32 0, i32 0, i1 false)')
        self.assertEqual(set(sa.parse('DXIL', text, _DXIL_RW).values()),
                         {sa.UNKNOWN})


if __name__ == '__main__':
    unittest.main()
