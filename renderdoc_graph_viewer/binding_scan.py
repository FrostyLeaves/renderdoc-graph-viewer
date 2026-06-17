# -*- coding: utf-8 -*-
"""Async shader-access refinement on the replay thread: ONE static whole-frame
pass via descriptor_access.refine (zero SetFrameEvent), replacing the old
~86ms/event GetAllUsedDescriptors walk. Reports {(eid, res_key):
'read'|'write'|'rw'} to the UI thread; entries it omits keep the conservative RW
double edge. Same job lifetime/cancellation pattern as ThumbnailJob."""

from . import descriptor_access


class ShaderAccessJob(object):
    def __init__(self, ctx, mqt, is_alive, on_done):
        """on_done(results, warnings) fires on the UI thread; results is
        {(eid, res_key): 'read'|'write'|'rw'} from one whole-frame static pass,
        warnings is a list (non-empty only when the pass genuinely failed, so
        the caller can surface it instead of mistaking it for an empty pass)."""
        self.ctx = ctx
        self.mqt = mqt
        self.is_alive = is_alive
        self.on_done = on_done

    def start(self):
        self.ctx.Replay().AsyncInvoke('rt_dep_graph_shaderaccess', self._run)

    def _run(self, controller):
        import renderdoc as rd
        results = {}
        warnings = []
        try:
            if self.is_alive():
                results = descriptor_access.refine(controller, rd,
                                                   warnings=warnings)
        except Exception:
            import traceback
            warnings.append('shader-access refinement crashed:\n'
                            + traceback.format_exc())
            results = {}
        cb = self.on_done
        self.mqt.InvokeOntoUIThread(
            lambda f=cb, r=results, w=list(warnings): f(r, w))
