# -*- coding: utf-8 -*-
"""Graph Viewer - qrenderdoc UI extension entry point.

Registers a Window-menu item opening a dockable pass <-> render-texture
dependency graph. The qrenderdoc import is guarded so plain-Python tooling
(unit tests, py_compile) can still import this package's submodules."""

try:
    import qrenderdoc as qrd
except ImportError:  # outside RenderDoc (unit tests / tooling)
    qrd = None

from . import graph_model
from . import navigation
from . import config as _config
from .ui import thumbnails
from .ui import status
from .i18n import tr

extiface_version = ''
cur_window = None


if qrd is not None:

    class GraphWindow(qrd.CaptureViewer):
        def __init__(self, ctx, version):
            super(GraphWindow, self).__init__()
            self.ctx = ctx
            self.version = version
            self.mqt = ctx.Extensions().GetMiniQtHelper()
            self.generation = 0
            self.graph = None
            self.bundle = None  # cached extraction; scope changes re-group locally
            self.nav = navigation.NavigationState()
            self._last_scope_key = ('__init__',)  # forces fit on first build
            self.thumb_job = None
            self.thumb_epoch = 0     # bumped per rebuild; retires stale jobs
            self.thumb_failed = set()  # SaveTexture failed; no retry
            self._thumb_pending = {}   # {key: autofit} queued while a job flies
            self._retired_jobs = []
            # static refinement payloads for the current capture+candidate set
            self._refinement_cache = None

            self.topWindow = self.mqt.CreateToplevelWidget(
                'Graph Viewer', lambda c, w, d: _window_closed())

            from .ui import graph_widget
            self.panel = graph_widget.GraphPanel({
                'refresh': self.refresh,
                'pass_clicked': self.on_pass_clicked,
                'resource_double_clicked': self.on_resource_double_clicked,
                'extract_apply': self._on_extract_apply,
                'features_apply': self._rebuild,
                'display_changed': self._on_display_changed,
                'member_event_jump': self.on_member_event_jump,
                'drill': self._on_drill,
                'navigate': self._on_navigate,
                'jump_scope': self._on_jump_scope,
                'back': self._on_back,
                'request_thumb': self._request_thumb,
            })
            lay = self.topWindow.layout()
            if lay is not None:
                lay.addWidget(self.panel)
            else:
                self.mqt.AddWidget(self.topWindow, self.panel)

            self.panel.set_breadcrumb([tr('Whole frame')])
            self.panel.set_graph(None)
            ctx.AddCaptureViewer(self)
            self.refresh()

        # -------------------------------------------------------- refresh

        def _on_back(self):
            if not self.nav.can_back:
                return
            view = self.nav.back()
            self.panel.set_back_enabled(self.nav.can_back)
            self._rebuild()
            self.panel.restore_view_state(view)

        def _on_drill(self, node):
            self.nav.drill(node, self.panel.capture_view_state())
            self.panel.set_back_enabled(self.nav.can_back)
            self._rebuild()

        def _on_navigate(self, index):
            # breadcrumb index 0 == whole-frame root
            self.nav.navigate(index, self.panel.capture_view_state())
            self.panel.set_back_enabled(self.nav.can_back)
            self._rebuild()

        def _on_jump_scope(self, node, focus_eid=None):
            # jump to the external scope a portal stands for; breadcrumb
            # becomes the target's ancestor chain. focus_eid: a member-row
            # double-click lands focused on that specific member's event.
            if self.bundle is None:
                return
            path = tuple(getattr(node, 'portal_path', ()) or ())
            chain = []
            if path:
                chain = graph_model.scope_chain(
                    self.bundle, path, node.first_eid)
                if not chain:
                    return  # could not resolve the instance ancestry
            self.nav.jump(chain, self.panel.capture_view_state())
            self.panel.set_back_enabled(self.nav.can_back)
            self._rebuild()
            feid = focus_eid
            if feid is None:
                feid = getattr(node, 'portal_focus_eid', None)
            if feid is not None:
                self.panel.focus_event(feid)

        def _on_extract_apply(self, candidates, display, features):
            # Refinement caches are built from exactly the admitted
            # resource set, so drop it whenever extraction inputs change.
            self._refinement_cache = None
            self.refresh(keep_scope=True, candidates=candidates,
                         display=display, features=features)

        def refresh(self, keep_scope=False, candidates=None, display=None,
                    features=None):
            # keep_scope keeps the active navigation stack during re-extraction
            self.generation += 1
            gen = self.generation
            self._cancel_thumbs()
            self.panel.clear_thumbnails()
            self.thumb_failed.clear()
            self.bundle = None
            if not keep_scope:
                self.nav.reset()
                self.panel.set_back_enabled(False)
                self._last_scope_key = ('__refresh__',)

            loaded = True
            try:
                loaded = bool(self.ctx.IsCaptureLoaded())
            except Exception:
                pass
            if not loaded:
                self.graph = None
                self.panel.set_graph(None)
                self.panel.set_status('')
                return

            if candidates is None:
                candidates = self.panel.candidate_config()
            if features is None:
                features = self.panel.feature_config()
            parse_shaders = bool(features.get(
                _config.KEY_PARSE_SHADER, _config.DEFAULTS[_config.KEY_PARSE_SHADER]))
            refinement_cache = self._refinement_cache
            self.panel.set_status(tr('Analyzing…'))

            def progress(done, total):
                def show():
                    if gen == self.generation:
                        self.panel.set_status(
                            tr('Analyzing… refining depth access %d/%d '
                               '(first time for this capture)')
                            % (done, total))
                self.mqt.InvokeOntoUIThread(show)

            def work(controller):
                try:
                    bundle = graph_model.extract_bundle(
                        controller, candidates=candidates,
                        refinement_cache=refinement_cache, progress=progress,
                        parse_shaders=parse_shaders)
                except Exception:
                    import traceback
                    err = traceback.format_exc()
                    self.mqt.InvokeOntoUIThread(lambda e=err: self._on_error(gen, e))
                    return
                self.mqt.InvokeOntoUIThread(
                    lambda b=bundle: self._on_bundle(
                        gen, b, candidates, display, features))

            self.ctx.Replay().AsyncInvoke('rt_dep_graph_extract', work)

        def _on_bundle(self, gen, bundle, candidates=None, display=None,
                       features=None):
            if gen != self.generation:
                return
            self.bundle = bundle
            self._refinement_cache = bundle.get('refinement_cache')
            if candidates is not None:
                # applied candidate snapshot for the landed bundle
                self.panel.set_candidates_applied(candidates)
            if display is not None:
                self.panel.set_display_applied(display)
            if features is not None:
                self.panel.set_features_applied(features)
            self._rebuild()

        def _rebuild(self):
            # re-group from the cached bundle; no replay round-trip
            if self.bundle is None:
                return
            # new scope/view generation for thumbnail jobs
            self.thumb_epoch += 1
            scope_path, scope_range = self.nav.current_scope()
            try:
                # scoped graph plus portal jump targets
                fg = graph_model.build_scoped(
                    self.bundle, scope_path, scope_range,
                    bundling=self.panel.bundling_enabled())
            except Exception:
                import traceback
                self._on_error(self.generation, traceback.format_exc())
                return
            self.graph = fg
            scope_key = (scope_path, scope_range)
            fit = scope_key != self._last_scope_key
            self._last_scope_key = scope_key
            self.panel.set_breadcrumb(
                [tr('Whole frame')] + self.nav.labels())
            self.panel.set_graph(fg, fit=fit)
            self._update_status()

        def _on_error(self, gen, err):
            if gen != self.generation:
                return
            self.panel.show_message(
                tr('Analysis failed (hover the status bar for details)'))
            self.panel.set_status(tr('Analysis failed'), warnings=[err])

        def _on_display_changed(self):
            self._update_status()

        def _update_status(self, extra=''):
            if self.graph is None:
                self.panel.set_status('')
                return
            # combine model and current-view warnings without mutating graph state
            warns = list(self.graph.warnings)
            warns += getattr(self.panel, 'layout_warnings', None) or []
            text = status.format_status(
                self.graph.stats, getattr(self.panel, 'hidden_counts', {}),
                warns, extra)
            self.panel.set_status(text, warnings=warns)

        # ----------------------------------------------------- thumbnails

        def _request_thumb(self, key, autofit):
            # queue if a job is in flight; _on_thumbs_done drains the next
            if self.graph is None:
                return
            self._thumb_pending[key] = autofit
            if self.thumb_job is None:
                self._drain_thumb_pending()

        def _drain_thumb_pending(self):
            # one node per job, so each carries its own autofit
            if self.graph is None or self.thumb_job is not None:
                return
            sel = thumbnails.select_next_thumb(
                self._thumb_pending,
                lambda k: self.panel.has_thumbnail(k) or k in self.thumb_failed,
                lambda k: self.graph.rid_objects.get(k[0]))
            if sel is None:
                return
            key, autofit, rid = sel
            gen = self.generation
            epoch = self.thumb_epoch
            restore = None
            try:
                restore = self.ctx.CurSelectedEvent()
            except Exception:
                pass
            self.panel.set_thumb_loading([key])
            job = thumbnails.ThumbnailJob(
                self.ctx, self.mqt, [(key, rid, key[1])], restore,
                is_alive=lambda: (gen == self.generation and
                                  epoch == self.thumb_epoch),
                on_thumb=lambda k, p: self._on_thumb(gen, k, p),
                on_done=lambda: self._on_thumbs_done(gen),
                autofit=autofit)
            self.thumb_job = job
            job.start()

        def _on_thumb(self, gen, key, path):
            if gen != self.generation:
                return  # bundle re-extracted: rid objects are stale
            if path is None:
                self.thumb_failed.add(key)  # depth/MSAA etc.: don't retry
                self.panel.set_thumb_failed(key)
                return
            # key is stable across scope views
            self.panel.set_thumbnail(key, path)

        def _on_thumbs_done(self, gen):
            job = self.thumb_job
            self.thumb_job = None
            if job is None:
                return
            if gen != self.generation:
                job.cleanup()
                return
            # keep the tmpdir: files are reloaded when the thumbnail
            # checkbox is toggled off/on; cleaned on next refresh
            self._retired_jobs.append(job)
            extra = ''
            if job.failed:
                extra = tr('%d thumbnails failed to grab') % job.failed
            self._update_status(extra)
            if self.graph is not None and self._thumb_pending:
                self._drain_thumb_pending()

        def _cancel_thumbs(self):
            # a bumped generation makes a running job's is_alive() false
            job = self.thumb_job
            self.thumb_job = None
            if job is not None:
                job.cleanup()
            for old in self._retired_jobs:
                old.cleanup()
            self._retired_jobs = []

        # ---------------------------------------------------- interaction

        def on_pass_clicked(self, node):
            try:
                self.ctx.SetEventID([], node.last_eid, node.last_eid)
            except Exception:
                pass

        def on_member_event_jump(self, eid):
            # double-clicked row of a bundled pass: land the UI there
            try:
                self.ctx.SetEventID([], eid, eid)
            except Exception:
                pass

        def on_resource_double_clicked(self, res_key):
            if self.graph is None:
                return
            rid = self.graph.rid_objects.get(res_key)
            if rid is None:
                return
            is_texture = False
            try:
                is_texture = self.ctx.GetTexture(rid) is not None
            except Exception:
                pass
            if is_texture:
                try:
                    import renderdoc as rd
                    self.ctx.ShowTextureViewer()
                    tv = self.ctx.GetTextureViewer()
                    try:
                        tv.ViewTexture(rid, rd.CompType.Typeless, True)
                    except TypeError:
                        tv.ViewTexture(rid, True)
                    return
                except Exception:
                    pass
            try:
                self.ctx.ShowResourceInspector()
                self.ctx.GetResourceInspector().Inspect(rid)
            except Exception:
                pass  # selection highlight still applies

        # -------------------------------------------------- CaptureViewer

        def OnCaptureLoaded(self):
            self._refinement_cache = None  # capture-local payloads
            self.panel.clear_expanded()
            self._thumb_pending = {}
            self.refresh()

        def OnCaptureClosed(self):
            self.generation += 1
            self._cancel_thumbs()
            self.thumb_failed.clear()
            self._refinement_cache = None
            self.graph = None
            self.bundle = None
            self.nav.reset()
            self.panel.set_back_enabled(False)
            self._last_scope_key = ('__closed__',)
            self.panel.set_breadcrumb([tr('Whole frame')])
            self.panel.clear_thumbnails()
            self.panel.set_graph(None)
            self.panel.set_status('')

        def OnSelectedEventChanged(self, event):
            pass

        def OnEventChanged(self, event):
            pass

        # --------------------------------------------------------- close

        def shutdown(self):
            self.generation += 1
            self._cancel_thumbs()
            self.ctx.RemoveCaptureViewer(self)

    def _window_closed():
        global cur_window
        if cur_window is not None:
            cur_window.shutdown()
        cur_window = None

    def open_window_callback(ctx, data):
        global cur_window
        if cur_window is None:
            try:
                from .ui import graph_widget  # noqa: F401 - probe PySide2
            except ImportError:
                ctx.Extensions().ErrorDialog(
                    'PySide2 is not available in this RenderDoc build.\n\n'
                    'Official builds from renderdoc.org bundle PySide2:\n'
                    'https://github.com/baldurk/renderdoc/wiki/PySide2',
                    'Graph Viewer')
                return
            cur_window = GraphWindow(ctx, extiface_version)
            ctx.AddDockWindow(cur_window.topWindow,
                              qrd.DockReference.MainToolArea, None)
        ctx.RaiseDockWindow(cur_window.topWindow)

    def register(version, ctx):
        global extiface_version
        extiface_version = version
        ctx.Extensions().RegisterWindowMenu(
            qrd.WindowMenu.Window, ['Graph Viewer'],
            open_window_callback)

    def unregister():
        global cur_window
        if cur_window is not None:
            mqt = cur_window.ctx.Extensions().GetMiniQtHelper()
            mqt.CloseToplevelWidget(cur_window.topWindow)
            cur_window = None

else:

    def register(version, ctx):
        raise RuntimeError('renderdoc_graph_viewer must run inside qrenderdoc')

    def unregister():
        pass
