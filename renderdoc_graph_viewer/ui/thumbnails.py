# -*- coding: utf-8 -*-
"""Async thumbnail batch on the replay thread: SaveTexture each resource,
post results to the UI thread progressively, restore the user's event."""

import os
import re
import shutil
import tempfile


def _safe(name):
    return re.sub(r'[^A-Za-z0-9_.-]', '_', name)


def select_next_thumb(pending, is_done, rid_of):
    """Return the next launchable thumbnail request from `pending`."""
    while pending:
        key, autofit = pending.popitem()
        if is_done(key):
            continue
        rid = rid_of(key)
        if rid is None:
            continue
        return key, autofit, rid
    return None


class ThumbnailJob(object):
    def __init__(self, ctx, mqt, items, restore_eid, is_alive, on_thumb,
                 on_done, autofit=False):
        """items: [(node_id, rid_object, eid)]. is_alive checked between
        items; a stale generation aborts the batch. on_thumb(node_id, path)
        / on_done() fire on the UI thread. autofit: fit black/white points
        to the texture's min/max so HDR/out-of-range content is visible."""
        self.ctx = ctx
        self.mqt = mqt
        self.items = sorted(items, key=lambda it: (it[2], it[0]))
        self.restore_eid = restore_eid
        self.is_alive = is_alive
        self.on_thumb = on_thumb
        self.on_done = on_done
        self.autofit = bool(autofit)
        self.tmpdir = None
        self.failed = 0

    def start(self):
        self.tmpdir = tempfile.mkdtemp(prefix='rt_dep_graph_')
        self.ctx.Replay().AsyncInvoke('rt_dep_graph_thumbs', self._run)

    def cleanup(self):
        if self.tmpdir is not None:
            shutil.rmtree(self.tmpdir, ignore_errors=True)
            self.tmpdir = None

    def _fit_range(self, controller, rd, ts, rid):
        """Best-effort: any failure leaves the default 0..1 range.
        GetMinMax runs on the GPU (~0ms)."""
        try:
            sub = rd.Subresource(0, 0, 0)
            mn, mx = controller.GetMinMax(rid, sub, rd.CompType.Typeless)
            # RGB only: a constant 0/1 alpha would swamp the range
            lo = min(mn.floatValue[:3])
            hi = max(mx.floatValue[:3])
            if hi > lo:
                ts.comp.blackPoint = float(lo)
                ts.comp.whitePoint = float(hi)
        except Exception:
            pass

    def _run(self, controller):
        import renderdoc as rd
        try:
            cur = None
            for node_id, rid, eid in self.items:
                if not self.is_alive():
                    break
                # per-item failure isolation
                ok = False
                path = None
                try:
                    if eid != cur:
                        controller.SetFrameEvent(eid, False)
                        cur = eid
                    # tuple key rendered as a filesystem-safe name
                    path = os.path.join(self.tmpdir or tempfile.gettempdir(),
                                        '%s.png' % _safe('%s' % (node_id,)))
                    ts = rd.TextureSave()
                    ts.resourceId = rid
                    ts.mip = 0
                    ts.slice.sliceIndex = 0
                    ts.alpha = rd.AlphaMapping.BlendToCheckerboard
                    # PNG, not JPG: qrenderdoc's Qt has no JPG decode
                    # plugin, so QPixmap(jpg) is always null
                    ts.destType = rd.FileType.PNG
                    if self.autofit:
                        self._fit_range(controller, rd, ts, rid)
                    ok = controller.SaveTexture(ts, path)
                except Exception:
                    ok = False
                cb = self.on_thumb
                if ok and path is not None and os.path.exists(path):
                    self.mqt.InvokeOntoUIThread(
                        lambda f=cb, n=node_id, p=path: f(n, p))
                else:
                    self.failed += 1
                    # path=None so the caller records the failure and won't retry
                    self.mqt.InvokeOntoUIThread(
                        lambda f=cb, n=node_id: f(n, None))
        finally:
            try:
                if self.restore_eid is not None:
                    controller.SetFrameEvent(self.restore_eid, True)
            except Exception:
                pass
            self.mqt.InvokeOntoUIThread(self.on_done)
