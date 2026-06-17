# -*- coding: utf-8 -*-
"""Graph Viewer - qrenderdoc UI extension entry point.

Registers a Window-menu item opening a dockable pass <-> render-texture
dependency graph. The qrenderdoc import is guarded so plain-Python tooling
(unit tests, py_compile) can still import this package's submodules."""

try:
    import qrenderdoc as qrd
except ImportError:  # outside RenderDoc (unit tests / tooling)
    qrd = None

from . import binding_scan
from . import graph_model
from . import thumbnails
from .i18n import tr

extiface_version = ''
cur_window = None

NAV_HISTORY_LIMIT = 50   # back-navigation snapshots kept (bounded)


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
            self.scope_stack = []  # [{label, path, range}]
            self.nav_history = []
            self._last_scope_key = ('__init__',)  # forces fit on first build
            self.thumb_job = None
            self.thumb_epoch = 0     # bumped per rebuild; retires stale jobs
            self.thumb_failed = set()  # SaveTexture failed; no retry
            self._thumb_pending = {}   # {key: autofit} queued while a job flies
            self._retired_jobs = []
            self.binding_job = None
            self.shader_access = {}  # (eid, res_key) -> 'unused'|'read'|'write'|'rw'
            # whether the whole-frame scan has completed; distinct from an empty
            # shader_access (a scan that resolved 0 verdicts -- e.g. a bindless
            # D3D12 capture -- is still "done" and must NOT be relaunched)
            self._binding_ready = False
            # diagnostics from the last shader-access pass; non-empty means the
            # static refinement genuinely failed (vs resolved 0 verdicts) and
            # the graph fell back to conservative RW edges
            self._binding_warnings = []
            # static depth-access pass, reused across re-extractions of the
            # same capture+candidate set; dropped on candidate change
            # (_on_candidates_apply) and capture switch
            self._depth_cache = None

            self.topWindow = self.mqt.CreateToplevelWidget(
                'Graph Viewer', lambda c, w, d: _window_closed())

            from . import graph_widget
            self.panel = graph_widget.GraphPanel({
                'refresh': self.refresh,
                'pass_clicked': self.on_pass_clicked,
                'resource_double_clicked': self.on_resource_double_clicked,
                'candidates_apply': self._on_candidates_apply,
                'bundling_toggled': lambda on: self._rebuild(),
                'display_changed': self._on_display_changed,
                'unused_toggled': self._on_unused_toggled,
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

        def _push_history(self):
            # snapshot the viewpoint too so back restores exact pan/zoom
            self.nav_history.append({
                'stack': list(self.scope_stack),
                'view': self.panel.capture_view_state(),
            })
            del self.nav_history[:-NAV_HISTORY_LIMIT]  # bounded
            self.panel.set_back_enabled(True)

        def _on_back(self):
            if not self.nav_history:
                return
            snap = self.nav_history.pop()
            self.scope_stack = snap['stack']
            self.panel.set_back_enabled(bool(self.nav_history))
            self._rebuild()
            self.panel.restore_view_state(snap['view'])

        def _on_drill(self, node):
            self._push_history()
            self.scope_stack.append({
                'label': node.name,
                'path': tuple(node.marker_path),
                'range': (node.first_eid, node.last_eid),
            })
            self._rebuild()

        def _on_navigate(self, index):
            # breadcrumb index 0 == whole-frame root
            self._push_history()
            del self.scope_stack[max(0, index):]
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
            self._push_history()
            self.scope_stack = chain
            self._rebuild()
            feid = focus_eid
            if feid is None:
                feid = getattr(node, 'portal_focus_eid', None)
            if feid is not None:
                self.panel.focus_event(feid)

        def _on_candidates_apply(self):
            # The candidate gate decides which resources reach usage_by_res,
            # and the depth-access cache is built from exactly that set. A
            # candidate change can make the cache incomplete (e.g. depth
            # targets re-admitted after a prior depth-off extraction), so drop
            # it and let the re-extraction recompute the static pass.
            self._depth_cache = None
            self.refresh(keep_scope=True)

        def refresh(self, keep_scope=False):
            # keep_scope=True: re-extract without losing navigation state;
            # only a capture change resets it
            self.generation += 1
            gen = self.generation
            self._cancel_thumbs()
            self.panel.clear_thumbnails()
            self.thumb_failed.clear()
            self.shader_access.clear()  # eids change with the capture
            self._binding_ready = False
            self._binding_warnings = []
            self.bundle = None
            if not keep_scope:
                del self.scope_stack[:]
                del self.nav_history[:]
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

            candidates = self.panel.candidate_config()
            depth_cache = self._depth_cache
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
                        depth_access=depth_cache, progress=progress)
                except Exception:
                    import traceback
                    err = traceback.format_exc()
                    self.mqt.InvokeOntoUIThread(lambda e=err: self._on_error(gen, e))
                    return
                self.mqt.InvokeOntoUIThread(
                    lambda b=bundle: self._on_bundle(gen, b, candidates))

            self.ctx.Replay().AsyncInvoke('rt_dep_graph_extract', work)

        def _on_bundle(self, gen, bundle, candidates=None):
            if gen != self.generation:
                return
            self.bundle = bundle
            self._depth_cache = bundle.get('depth_access')
            if candidates is not None:
                # mark applied only on success; a failed extraction keeps
                # the pending-apply hint alive
                self.panel.set_candidates_applied(candidates)
            self._rebuild()

        def _rebuild(self):
            # re-group from the cached bundle; no replay round-trip
            if self.bundle is None:
                return
            # retire the previous scope's thumbnail batch; _on_thumbs_done
            # relaunches for the new scope's gaps
            self.thumb_epoch += 1
            if self.scope_stack:
                cur = self.scope_stack[-1]
                scope_path, scope_range = cur['path'], cur['range']
            else:
                scope_path, scope_range = (), None
            try:
                # bundling lives in the parse layer: graph, portal targets
                # and jump focus all come from one merged result
                sa = self.shader_access if self.panel.act_unused.isChecked() else {}
                fg = graph_model.build_scoped(
                    self.bundle, scope_path, scope_range,
                    bundling=self.panel.bundling_enabled(),
                    shader_access=sa)
                # cached verdicts dash edges now; gaps scanned below
                graph_model.apply_binding_usage(fg, sa)
            except Exception:
                import traceback
                self._on_error(self.generation, traceback.format_exc())
                return
            self.graph = fg
            scope_key = (scope_path, scope_range)
            fit = scope_key != self._last_scope_key
            self._last_scope_key = scope_key
            self.panel.set_breadcrumb(
                [tr('Whole frame')] + [s['label'] for s in self.scope_stack])
            self.panel.set_graph(fg, fit=fit)
            self._update_status()
            if self.panel.act_unused.isChecked():
                self._start_binding_scan()

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
            s = self.graph.stats
            text = '%d passes · %d resources · %d edges · %.2fs' % (
                s.get('passes', 0), s.get('resources', 0),
                s.get('edges', 0), s.get('seconds', 0.0))
            hc = getattr(self.panel, 'hidden_counts', {})
            parts = []
            if hc.get('orphans'):
                parts.append(tr('orphans %d') % hc['orphans'])
            if hc.get('external'):
                parts.append(tr('external %d') % hc['external'])
            if hc.get('internal'):
                parts.append(tr('internal %d') % hc['internal'])
            if parts:
                text += tr(' · hidden: ') + u' / '.join(parts)
            # union (never mutate) the model, layout and shader-access warnings;
            # layout/binding live off the graph so revisiting a view can't
            # double-count them
            warns = list(self.graph.warnings)
            warns += getattr(self.panel, 'layout_warnings', None) or []
            warns += self._binding_warnings
            if warns:
                text += u' · %d warnings' % len(warns)
            if extra:
                text += ' · ' + extra
            self.panel.set_status(text, warnings=warns)

        # ----------------------------------------------- unused bindings

        def _on_unused_toggled(self, on):
            if self.graph is not None:
                self._rebuild()
            if on:
                self._start_binding_scan()

        def _start_binding_scan(self):
            # ONE static whole-frame pass (zero replay, descriptor_access.refine
            # — replaces the old ~86ms/event SetFrameEvent walk). Computed once
            # per capture, cached across scope navigation, cleared by refresh.
            if (self.graph is None or self.binding_job is not None or
                    not self.panel.act_unused.isChecked() or self._binding_ready):
                return
            gen = self.generation
            self._update_status(tr('Refining read/write access…'))
            job = binding_scan.ShaderAccessJob(
                self.ctx, self.mqt,
                is_alive=lambda: (gen == self.generation and
                                  self.panel.act_unused.isChecked()),
                on_done=lambda res, w: self._on_binding_scan_done(gen, res, w))
            self.binding_job = job
            job.start()

        def _on_binding_scan_done(self, gen, results, warnings=()):
            self.binding_job = None
            if gen != self.generation:
                return  # re-extracted: eids/rids are stale
            self.shader_access = results   # whole-frame static result, complete
            self._binding_warnings = list(warnings)
            # latch ready even on failure (and even on a 0-verdict result): the
            # failure is deterministic, so an auto-relaunch would just loop.
            # Re-analyze clears _binding_ready and retries; the warning tells the
            # user the graph fell back to conservative RW edges meanwhile.
            self._binding_ready = True
            if self.graph is not None:
                self._rebuild()   # rebuild applies the de-edge refinement
            self._update_status()

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
            while self._thumb_pending:
                key, autofit = self._thumb_pending.popitem()
                if self.panel.has_thumbnail(key) or key in self.thumb_failed:
                    continue
                rid = self.graph.rid_objects.get(key[0])
                if rid is None:
                    continue
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
                return

        def _on_thumb(self, gen, key, path):
            if gen != self.generation:
                return  # bundle re-extracted: rid objects are stale
            if path is None:
                self.thumb_failed.add(key)  # depth/MSAA etc.: don't retry
                self.panel.set_thumb_failed(key)
                return
            # keys are stable across scopes: an old-scope grab still applies
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
            self._depth_cache = None  # eids/previews belong to the old capture
            self.panel.clear_expanded()
            self._thumb_pending = {}
            self.refresh()

        def OnCaptureClosed(self):
            self.generation += 1
            self._cancel_thumbs()
            self.thumb_failed.clear()
            self.shader_access.clear()
            self._binding_ready = False
            self._binding_warnings = []
            self._depth_cache = None
            self.graph = None
            self.bundle = None
            del self.scope_stack[:]
            del self.nav_history[:]
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
                from . import graph_widget  # noqa: F401 - probe PySide2
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
