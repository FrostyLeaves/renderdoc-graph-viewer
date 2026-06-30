# -*- coding: utf-8 -*-
"""ThumbnailJob tests with fake replay/UI plumbing."""

import unittest

from tests import rd_stub

# shared renderdoc stub for the lazy import in _run
rd_stub.install()

from renderdoc_graph_viewer.ui.thumbnails import (ThumbnailJob,  # noqa: E402
                                               select_next_thumb)


class FakeMqt(object):
    def InvokeOntoUIThread(self, fn):
        fn()  # synchronous for tests


class FakeController(object):
    def __init__(self, fail_rids=()):
        self.fail_rids = set(fail_rids)
        self.events = []
        self.saved_comp = []   # (blackPoint, whitePoint) per SaveTexture

    def SetFrameEvent(self, eid, force):
        self.events.append(eid)

    def GetMinMax(self, rid, sub, typeCast):
        import types
        mn = types.SimpleNamespace(floatValue=[0.1, 0.2, 0.3, 1.0])
        mx = types.SimpleNamespace(floatValue=[8.0, 6.0, 4.0, 1.0])
        return mn, mx

    def SaveTexture(self, ts, path):
        self.saved_comp.append((ts.comp.blackPoint, ts.comp.whitePoint))
        if ts.resourceId in self.fail_rids:
            raise RuntimeError('save failed')
        with open(path, 'wb') as f:
            f.write(b'jpg')
        return True


def run_job(items, controller, autofit=False):
    """Drive _run synchronously; returns [(key, path-or-None)] callbacks."""
    got = []
    job = ThumbnailJob(
        ctx=None, mqt=FakeMqt(), items=items, restore_eid=None,
        is_alive=lambda: True,
        on_thumb=lambda key, path: got.append((key, path)),
        on_done=lambda: None, autofit=autofit)
    import tempfile
    job.tmpdir = tempfile.mkdtemp(prefix='rt_dep_test_')
    try:
        job._run(controller)
    finally:
        job.cleanup()
    return got, job


class TestThumbnailJob(unittest.TestCase):
    def test_tuple_keys_produce_files(self):
        # tuple thumbnail keys
        items = [(('res100', 50), 'rid100', 50),
                 (('res200', 60), 'rid200', 60)]
        got, job = run_job(items, FakeController())
        self.assertEqual(job.failed, 0)
        self.assertEqual(len(got), 2)
        self.assertEqual(got[0][0], ('res100', 50))
        # callback paths exist during callback execution
        self.assertTrue(all(p is not None for _k, p in got))
        # PNG is the only format the bundled Qt can decode
        self.assertTrue(all(p.endswith('.png') for _k, p in got))

    def test_one_failure_does_not_kill_the_batch(self):
        items = [(('bad', 10), 'rid_bad', 10),
                 (('good', 20), 'rid_good', 20)]
        got, job = run_job(items, FakeController(fail_rids={'rid_bad'}))
        self.assertEqual(job.failed, 1)
        by_key = dict(got)
        self.assertIsNone(by_key[('bad', 10)])       # failure reported
        self.assertIsNotNone(by_key[('good', 20)])   # batch continued

    def test_set_frame_event_exception_is_per_item(self):
        class Boom(FakeController):
            def SetFrameEvent(self, eid, force):
                if eid == 10:
                    raise RuntimeError('replay error')
                FakeController.SetFrameEvent(self, eid, force)

        items = [(('a', 10), 'rid_a', 10), (('b', 20), 'rid_b', 20)]
        got, job = run_job(items, Boom())
        self.assertEqual(job.failed, 1)
        by_key = dict(got)
        self.assertIsNone(by_key[('a', 10)])
        self.assertIsNotNone(by_key[('b', 20)])

    def test_autofit_sets_range_from_rgb_minmax(self):
        # autofit fits black/white to the texture's RGB min/max so HDR /
        # out-of-[0,1] content is visible; alpha is excluded
        items = [(('hdr', 5), 'rid_hdr', 5)]
        ctl = FakeController()
        run_job(items, ctl, autofit=True)
        self.assertEqual(ctl.saved_comp[0], (0.1, 8.0))

    def test_autofit_off_keeps_default_range(self):
        items = [(('ldr', 5), 'rid_ldr', 5)]
        ctl = FakeController()
        run_job(items, ctl, autofit=False)
        self.assertEqual(ctl.saved_comp[0], (0.0, 1.0))


class TestSelectNextThumb(unittest.TestCase):
    def test_empty_pending_returns_none(self):
        self.assertIsNone(
            select_next_thumb({}, lambda k: False, lambda k: 'rid'))

    def test_returns_launchable_and_consumes_it(self):
        pending = {'a': True, 'b': False}   # popitem() pops 'b' (LIFO) first
        sel = select_next_thumb(pending, lambda k: False,
                                lambda k: 'rid_%s' % k)
        self.assertEqual(sel, ('b', False, 'rid_b'))
        self.assertEqual(pending, {'a': True})   # only 'b' consumed

    def test_skips_done_keys(self):
        pending = {'done': False, 'go': False}
        sel = select_next_thumb(pending, lambda k: k == 'done',
                                lambda k: 'r')
        self.assertEqual(sel[0], 'go')

    def test_drops_keys_with_no_rid(self):
        pending = {'x': True}
        self.assertIsNone(
            select_next_thumb(pending, lambda k: False, lambda k: None))
        self.assertEqual(pending, {})   # popped and dropped


if __name__ == '__main__':
    unittest.main()
