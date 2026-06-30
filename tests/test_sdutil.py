# -*- coding: utf-8 -*-
"""_sdutil structured-data child accessors: the defensive coercion/degradation
paths that keep one malformed chunk from aborting a whole-frame static walk."""
import unittest

from renderdoc_graph_viewer.parse import _sdutil as sd

NULL = 'ResourceId::0'


class _SD(object):
    """Minimal SDObject: name + value + named children, with AsInt/AsString/
    AsResourceId coercions that raise on a value of the wrong shape."""

    def __init__(self, name, value=None, children=None, rid=None):
        self.name = name
        self._v = value
        self._rid = rid
        self._kids = list(children or [])

    def NumChildren(self):
        return len(self._kids)

    def GetChild(self, i):
        return self._kids[i]

    def FindChild(self, name):
        for k in self._kids:
            if k.name == name:
                return k
        return None

    def AsInt(self):
        return int(self._v)

    def AsString(self):
        if self._v is None:
            raise ValueError('not a string')
        return str(self._v)

    def AsResourceId(self):
        if self._rid is None:
            raise ValueError('not a resource id')
        return self._rid


class TestNameHelpers(unittest.TestCase):
    def test_base_strips_namespace(self):
        self.assertEqual(sd._base('vk::CreateImage'), 'CreateImage')
        self.assertEqual(sd._base('CreateImage'), 'CreateImage')
        self.assertEqual(sd._base('a::b::c'), 'c')

    def test_chunk_is_matches_bare_and_namespaced_only(self):
        self.assertTrue(sd._chunk_is('Draw', 'Draw'))
        self.assertTrue(sd._chunk_is('vk::Draw', 'Draw'))
        self.assertFalse(sd._chunk_is('vk::Dispatch', 'Draw'))
        self.assertFalse(sd._chunk_is('Predraw', 'draw'))


class TestRidStr(unittest.TestCase):
    def test_null_and_empty_become_none(self):
        self.assertIsNone(sd._rid_str(None))
        self.assertIsNone(sd._rid_str(NULL))
        self.assertIsNone(sd._rid_str(''))

    def test_real_id_passes_through(self):
        self.assertEqual(sd._rid_str('ResourceId::7'), 'ResourceId::7')


class TestChildInt(unittest.TestCase):
    def test_missing_parent_or_child_returns_default(self):
        self.assertEqual(sd._ci(None, 'x'), 0)
        self.assertEqual(sd._ci(None, 'x', default=9), 9)
        self.assertEqual(sd._ci(_SD('p'), 'x', default=3), 3)

    def test_present_int_child(self):
        self.assertEqual(sd._ci(_SD('p', children=[_SD('x', 5)]), 'x'), 5)

    def test_uncoercible_present_child_degrades(self):
        p = _SD('p', children=[_SD('x', 'not-an-int')])
        self.assertEqual(sd._ci(p, 'x', default=-1), -1)


class TestChildStr(unittest.TestCase):
    def test_missing_child_is_empty(self):
        self.assertEqual(sd._cstr(_SD('p'), 'x'), '')

    def test_present_string(self):
        self.assertEqual(sd._cstr(_SD('p', children=[_SD('x', 'hi')]), 'x'), 'hi')

    def test_uncoercible_present_child_degrades(self):
        p = _SD('p', children=[_SD('x', None)])  # AsString raises
        self.assertEqual(sd._cstr(p, 'x'), '')


class TestChildRid(unittest.TestCase):
    def test_missing_child_is_none(self):
        self.assertIsNone(sd._crid_obj(_SD('p'), 'x'))
        self.assertIsNone(sd._crid(_SD('p'), 'x'))

    def test_present_rid(self):
        p = _SD('p', children=[_SD('x', rid='ResourceId::4')])
        self.assertEqual(sd._crid_obj(p, 'x'), 'ResourceId::4')
        self.assertEqual(sd._crid(p, 'x'), 'ResourceId::4')

    def test_null_rid_child_is_canonicalised_away_by_crid(self):
        p = _SD('p', children=[_SD('x', rid=NULL)])
        self.assertEqual(sd._crid_obj(p, 'x'), NULL)  # raw object kept
        self.assertIsNone(sd._crid(p, 'x'))           # canonical str drops null


class TestSelfRid(unittest.TestCase):
    def test_real_id_probe_failure_and_null(self):
        self.assertEqual(sd._self_rid(_SD('e', rid='ResourceId::2')),
                         'ResourceId::2')
        self.assertIsNone(sd._self_rid(_SD('e')))        # AsResourceId raises
        self.assertIsNone(sd._self_rid(_SD('e', rid=NULL)))


class TestLastResourceId(unittest.TestCase):
    def test_returns_last_non_null_id(self):
        ch = _SD('Create', children=[
            _SD('a', rid='ResourceId::1'),
            _SD('b', 7),                # not an id: AsResourceId raises
            _SD('c', rid='ResourceId::3'),
            _SD('d', rid=NULL),         # null: ignored
        ])
        self.assertEqual(sd._last_resource_id(ch), 'ResourceId::3')

    def test_no_ids_returns_none(self):
        ch = _SD('Create', children=[_SD('a', 1), _SD('b', 2)])
        self.assertIsNone(sd._last_resource_id(ch))


if __name__ == '__main__':
    unittest.main()
