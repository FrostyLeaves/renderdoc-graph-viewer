# -*- coding: utf-8 -*-
"""shader_refinement.refine dispatcher tests."""
import types
import unittest

from tests import rd_stub
rd = rd_stub.install()

from renderdoc_graph_viewer.parse import shader_refinement


def _raise(*_a, **_k):
    raise RuntimeError('boom')


def _ctl(api='Vulkan', raise_api=False):
    """Minimal controller exposing just GetAPIProperties().pipelineType."""
    def get_props():
        if raise_api:
            raise RuntimeError('no pipelineType')
        return types.SimpleNamespace(pipelineType='GraphicsAPI.%s' % api)
    return types.SimpleNamespace(GetAPIProperties=get_props)


class TestRefineDispatch(unittest.TestCase):
    def test_api_type_unavailable_warns_and_returns_empty(self):
        warns = []
        out = shader_refinement.refine(_ctl(raise_api=True), rd, warnings=warns)
        self.assertEqual(out, {})
        self.assertEqual(len(warns), 1)
        self.assertIn('API type unavailable', warns[0])

    def test_api_without_refiner_is_silent(self):
        # OpenGL has no registered refine pass: {} and no warning.
        warns = []
        out = shader_refinement.refine(_ctl(api='OpenGL'), rd, warnings=warns)
        self.assertEqual(out, {})
        self.assertEqual(warns, [])

    def test_refiner_exception_warns_and_returns_empty(self):
        # walker failure path
        orig = shader_refinement._walk_executables
        shader_refinement._walk_executables = _raise
        try:
            warns = []
            out = shader_refinement.refine(_ctl(api='Vulkan'), rd, warnings=warns)
        finally:
            shader_refinement._walk_executables = orig
        self.assertEqual(out, {})
        self.assertEqual(len(warns), 1)
        self.assertIn('refinement failed', warns[0])

    def test_structured_data_unavailable_returns_empty(self):
        # Real walk, but the structured file is unavailable: still {} + warning.
        ctl = _ctl(api='Vulkan')
        ctl.GetStructuredFile = _raise
        warns = []
        out = shader_refinement.refine(ctl, rd, warnings=warns)
        self.assertEqual(out, {})
        self.assertEqual(len(warns), 1)
        self.assertIn('conservative', warns[0])

    def test_tolerates_no_warnings_list(self):
        # warnings=None path
        self.assertEqual(shader_refinement.refine(_ctl(raise_api=True), rd), {})
        self.assertEqual(shader_refinement.refine(_ctl(api='OpenGL'), rd), {})


if __name__ == '__main__':
    unittest.main()
