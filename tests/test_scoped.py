# -*- coding: utf-8 -*-
"""Focus-navigation mode: each view shows ONE level of children
inside the current scope (a marker instance = path + contiguous eid range);
double-click drills in, breadcrumbs navigate back."""

import unittest

from renderdoc_graph_viewer.graph_model import (build_scoped, scope_chain,
                                             _leaf_runs, _run_containing)
from tests.fakes import draw, dispatch

RES_INFO = {
    'GB': {'kind': 'color', 'info': {}},
    'HDR': {'kind': 'color', 'info': {}},
    'SM': {'kind': 'depth', 'info': {}},
    'SC': {'kind': 'swapchain', 'info': {}},
}


def _bundle(leaves, usage):
    return {
        'leaves': leaves,
        'usage_by_res': usage,
        'res_info': RES_INFO,
        'res_names': {'GB': 'GB', 'HDR': 'HDR', 'SM': 'SM', 'SC': 'SC'},
        'rid_objects': {},
        'warnings': [],
        'seconds': 0.0,
    }


def _two_camera_bundle():
    # camera A (eid 10-30), Fluid (40), camera B (50-60): same marker name
    leaves = [
        draw(10, ('GB',), markers=('Frame', 'Camera', 'Gbuffer')),
        draw(20, ('HDR',), markers=('Frame', 'Camera', 'Lighting')),
        draw(30, ('HDR',), markers=('Frame', 'Camera', 'Post')),
        dispatch(40, markers=('Frame', 'Fluid')),
        draw(50, ('GB',), markers=('Frame', 'Camera', 'Gbuffer')),
        draw(60, ('HDR',), markers=('Frame', 'Camera', 'Lighting')),
    ]
    usage = {
        'GB': [(10, 'ColorTarget'), (20, 'PS_Resource'),
               (50, 'ColorTarget'), (60, 'PS_Resource')],
        'HDR': [(20, 'ColorTarget'), (30, 'PS_Resource'),
                (30, 'ColorTarget'), (60, 'ColorTarget')],
    }
    return _bundle(leaves, usage)


class TestRootScope(unittest.TestCase):
    def test_root_shows_level1_instances(self):
        fg = build_scoped(_two_camera_bundle(), (), None)
        self.assertEqual([p.name for p in fg.passes], ['Frame'])
        self.assertTrue(fg.passes[0].drillable)

    def test_level2_instances_split_by_contiguity(self):
        bundle = _two_camera_bundle()
        frame = build_scoped(bundle, (), None).passes[0]
        fg = build_scoped(bundle, frame.marker_path,
                          (frame.first_eid, frame.last_eid))
        self.assertEqual([p.name for p in fg.passes],
                         ['Camera', 'Fluid', 'Camera #2'])
        cam_a, fluid, cam_b = fg.passes
        self.assertEqual((cam_a.first_eid, cam_a.last_eid), (10, 30))
        self.assertEqual((cam_b.first_eid, cam_b.last_eid), (50, 60))
        self.assertTrue(cam_a.drillable)
        self.assertTrue(cam_b.drillable)
        self.assertFalse(fluid.drillable)  # single leaf, no deeper markers


class TestDrilledScope(unittest.TestCase):
    def _camera_a(self):
        bundle = _two_camera_bundle()
        frame = build_scoped(bundle, (), None).passes[0]
        lvl2 = build_scoped(bundle, frame.marker_path,
                            (frame.first_eid, frame.last_eid))
        cam_a = lvl2.passes[0]
        return bundle, build_scoped(
            bundle, cam_a.marker_path, (cam_a.first_eid, cam_a.last_eid))

    def test_scope_shows_only_own_children(self):
        _bundle_, fg = self._camera_a()
        own = [p for p in fg.passes if p.kind != 'portal']
        self.assertEqual([p.name for p in own],
                         ['Gbuffer', 'Lighting', 'Post'])

    def test_camera_b_content_excluded(self):
        _bundle_, fg = self._camera_a()
        for p in fg.passes:
            if p.kind == 'portal':
                continue  # portals deliberately reference external ranges
            self.assertLessEqual(p.last_eid, 30)

    def test_edges_within_scope(self):
        _bundle_, fg = self._camera_a()
        by_key = {}
        for n in fg.resources:
            by_key.setdefault(n.res_key, []).append(n)
        gb = by_key['GB'][0]
        portal_ids = set(p.id for p in fg.passes if p.kind == 'portal')
        own_edges = set(
            (e.src_id, e.dst_id, e.kind) for e in fg.edges
            if gb.id in (e.src_id, e.dst_id)
            and e.src_id not in portal_ids and e.dst_id not in portal_ids)
        self.assertEqual(
            own_edges,
            {(fg.passes[0].id, gb.id, 'write'), (gb.id, fg.passes[1].id, 'read')})

    def test_outside_activity_annotated(self):
        _bundle_, fg = self._camera_a()
        gb = [n for n in fg.resources if n.res_key == 'GB'][0]
        # GB is also written/read by camera B (eid 50/60), outside this scope
        self.assertEqual(gb.outside_writers, 1)
        self.assertEqual(gb.outside_readers, 1)


class TestSelfRWInScope(unittest.TestCase):
    def test_outside_writer_keeps_self_read_as_scope_input(self):
        # HZB written outside this scope, then read+written inside by one
        # node: the read is a real cross-scope relationship - keep it
        leaves = [
            dispatch(10, markers=('Frame', 'Prepare')),
            dispatch(20, markers=('Frame', 'Camera', 'Refine')),
        ]
        usage = {
            'M': [(10, 'CS_RWResource'), (20, 'CS_RWResource')],
        }
        bundle = _bundle(leaves, usage)
        frame = build_scoped(bundle, (), None).passes[0]
        lvl2 = build_scoped(bundle, frame.marker_path,
                            (frame.first_eid, frame.last_eid))
        cam = [p for p in lvl2.passes if p.name == 'Camera'][0]
        fg = build_scoped(bundle, cam.marker_path,
                          (cam.first_eid, cam.last_eid))
        m_nodes = [n for n in fg.resources if n.res_key == 'M']
        self.assertTrue(any(n.scope_input for n in m_nodes))
        portal_ids = set(p.id for p in fg.passes if p.kind == 'portal')
        read_edges = [e for e in fg.edges if e.kind == 'read'
                      and any(e.src_id == n.id for n in m_nodes)
                      and e.dst_id not in portal_ids]
        self.assertEqual(len(read_edges), 1)
        # external RW produces the scoped input only
        portal_writes = [e for e in fg.edges if e.kind == 'write'
                         and e.src_id in portal_ids]
        self.assertEqual(len(portal_writes), 1)
        portal_reads = [e for e in fg.edges if e.kind == 'read'
                        and e.dst_id in portal_ids]
        self.assertEqual(len(portal_reads), 0)

    def test_no_writer_anywhere_collapses_to_output(self):
        leaves = [
            dispatch(20, markers=('Frame', 'Camera', 'Refine')),
        ]
        usage = {
            'M': [(20, 'CS_RWResource')],
        }
        bundle = _bundle(leaves, usage)
        frame = build_scoped(bundle, (), None).passes[0]
        lvl2 = build_scoped(bundle, frame.marker_path,
                            (frame.first_eid, frame.last_eid))
        cam = [p for p in lvl2.passes if p.name == 'Camera'][0]
        fg = build_scoped(bundle, cam.marker_path,
                          (cam.first_eid, cam.last_eid))
        self.assertEqual(len(fg.resources), 1)
        self.assertEqual([e.kind for e in fg.edges], ['write'])


class TestPortalTiming(unittest.TestCase):
    """Portal edge timing tests."""

    def _camera(self, leaves, usage):
        bundle = _bundle(leaves, usage)
        frame = build_scoped(bundle, (), None).passes[0]
        lvl2 = build_scoped(bundle, frame.marker_path,
                            (frame.first_eid, frame.last_eid))
        cam = [p for p in lvl2.passes if p.name == 'Camera'][0]
        return build_scoped(bundle, cam.marker_path,
                            (cam.first_eid, cam.last_eid))

    def test_producer_portal_excludes_writes_after_scope(self):
        # outside writer ahead of the scope, later overwrite after it
        fg = self._camera(
            [draw(10, ('GB',), markers=('Frame', 'Shadow')),
             draw(20, ('HDR',), markers=('Frame', 'Camera', 'Lighting')),
             draw(30, ('GB',), markers=('Frame', 'Post'))],
            {'GB': [(10, 'ColorTarget'), (20, 'PS_Resource'),
                    (30, 'ColorTarget')],
             'HDR': [(20, 'ColorTarget')]})
        producers = set(p.portal_path for p in fg.passes
                        if p.kind == 'portal' and p.portal_role == 'producer')
        self.assertIn(('Frame', 'Shadow'), producers)
        self.assertNotIn(('Frame', 'Post'), producers)

    def test_consumer_portal_excludes_reads_before_scope(self):
        # outside readers on both sides of the scoped write
        fg = self._camera(
            [draw(10, ('HDR',), markers=('Frame', 'Pre')),
             draw(20, ('GB',), markers=('Frame', 'Camera', 'Gbuffer')),
             draw(30, ('HDR',), markers=('Frame', 'Post'))],
            {'GB': [(10, 'PS_Resource'), (20, 'ColorTarget'),
                    (30, 'PS_Resource')],
             'HDR': [(10, 'ColorTarget'), (30, 'ColorTarget')]})
        consumers = set(p.portal_path for p in fg.passes
                        if p.kind == 'portal' and p.portal_role == 'consumer')
        self.assertIn(('Frame', 'Post'), consumers)
        self.assertNotIn(('Frame', 'Pre'), consumers)


class TestScopePortals(unittest.TestCase):
    """External scopes that touch a resource appear as portal nodes;
    double-click jumps there (window rebuilds the breadcrumb chain)."""

    def _camera_a(self, bundle):
        frame = build_scoped(bundle, (), None).passes[0]
        lvl2 = build_scoped(bundle, frame.marker_path,
                            (frame.first_eid, frame.last_eid))
        cam_a = lvl2.passes[0]
        return build_scoped(bundle, cam_a.marker_path,
                            (cam_a.first_eid, cam_a.last_eid))

    def test_external_consumer_becomes_portal(self):
        bundle = _two_camera_bundle()
        fg = self._camera_a(bundle)
        portals = [p for p in fg.passes if p.kind == 'portal']
        self.assertEqual(len(portals), 1)
        portal = portals[0]
        # camera B shares the path but is a different instance
        self.assertEqual(portal.portal_path, ('Frame', 'Camera'))
        self.assertEqual(portal.portal_range, (50, 60))
        gb = [n for n in fg.resources if n.res_key == 'GB'][-1]
        self.assertIn((gb.id, portal.id, 'read'),
                      set((e.src_id, e.dst_id, e.kind) for e in fg.edges))

    def test_external_producer_portal_feeds_scope_input(self):
        leaves = [
            draw(10, ('SM',), markers=('Frame', 'Shadow')),
            draw(20, ('HDR',), markers=('Frame', 'Camera', 'Lighting')),
        ]
        usage = {
            'SM': [(10, 'ColorTarget'), (20, 'PS_Resource')],
            'HDR': [(20, 'ColorTarget')],
        }
        bundle = _bundle(leaves, usage)
        frame = build_scoped(bundle, (), None).passes[0]
        lvl2 = build_scoped(bundle, frame.marker_path,
                            (frame.first_eid, frame.last_eid))
        cam = [p for p in lvl2.passes if p.name == 'Camera'][0]
        fg = build_scoped(bundle, cam.marker_path,
                          (cam.first_eid, cam.last_eid))
        portals = [p for p in fg.passes if p.kind == 'portal']
        self.assertEqual(len(portals), 1)
        portal = portals[0]
        self.assertEqual(portal.portal_path, ('Frame', 'Shadow'))
        sm = [n for n in fg.resources if n.res_key == 'SM'][0]
        self.assertTrue(sm.scope_input)
        self.assertIn((portal.id, sm.id, 'write'),
                      set((e.src_id, e.dst_id, e.kind) for e in fg.edges))

    def test_portals_deduplicate_per_instance(self):
        leaves = [
            draw(10, ('GB',), markers=('Frame', 'Camera', 'Gbuffer')),
            draw(20, ('HDR',), markers=('Frame', 'Camera', 'Lighting')),
            dispatch(35, markers=('Frame', 'Fluid')),  # splits the instances
            draw(50, ('SM',), markers=('Frame', 'Camera', 'Lighting')),
        ]
        usage = {
            'GB': [(10, 'ColorTarget'), (50, 'PS_Resource')],
            'HDR': [(20, 'ColorTarget'), (50, 'PS_Resource')],
            'SM': [(50, 'ColorTarget')],
        }
        bundle = _bundle(leaves, usage)
        fg = self._camera_a(bundle)
        # GB and HDR are both consumed by camera B -> one portal, two edges
        portals = [p for p in fg.passes if p.kind == 'portal']
        self.assertEqual(len(portals), 1)
        portal = portals[0]
        reads = [e for e in fg.edges if e.dst_id == portal.id]
        self.assertEqual(len(reads), 2)

    def test_producer_and_consumer_portals_stay_acyclic(self):
        # separate producer and consumer portal roles
        leaves = [
            dispatch(10, markers=('Frame', 'Prepare')),
            draw(20, ('HDR',), markers=('Frame', 'Camera', 'Lighting')),
            dispatch(50, markers=('Frame', 'Other')),
        ]
        usage = {
            'SM': [(10, 'ColorTarget'), (20, 'PS_Resource')],
            'HDR': [(20, 'ColorTarget'), (50, 'PS_Resource')],
        }
        bundle = _bundle(leaves, usage)
        frame = build_scoped(bundle, (), None).passes[0]
        lvl2 = build_scoped(bundle, frame.marker_path,
                            (frame.first_eid, frame.last_eid))
        cam = [p for p in lvl2.passes if p.name == 'Camera'][0]
        fg = build_scoped(bundle, cam.marker_path,
                          (cam.first_eid, cam.last_eid))
        portals = [p for p in fg.passes if p.kind == 'portal']
        roles = sorted(getattr(p, 'portal_role', '?') for p in portals)
        self.assertEqual(roles, ['consumer', 'producer'])
        # and the rank-edge set stays acyclic (clean layout, no fallback)
        from renderdoc_graph_viewer.layout import graph_layout
        sizes = dict((n.id, (100.0, 40.0)) for n in fg.nodes())
        _pos, warns = graph_layout.compute_layout(
            fg.nodes(), fg.edges, sizes, rank_edges=fg.rank_edges)
        self.assertEqual(warns, [])

    def test_scope_chain_builds_breadcrumb_ancestry(self):
        bundle = _two_camera_bundle()
        chain = scope_chain(bundle, ('Frame', 'Camera'), 55)
        self.assertEqual([c['label'] for c in chain],
                         ['Frame', 'Camera'])
        self.assertEqual(chain[0]['path'], ('Frame',))
        self.assertEqual(chain[1]['path'], ('Frame', 'Camera'))
        self.assertEqual(chain[1]['range'], (50, 60))


class TestInternalFlag(unittest.TestCase):
    """Internal resource flag tests."""

    def _scoped(self, usage, leaves=None):
        leaves = leaves or [
            dispatch(10, markers=('Frame', 'FSR')),
            draw(20, ('HDR',), markers=('Frame', 'Out')),
        ]
        bundle = _bundle(leaves, usage)
        # drill into the Frame instance so FSR and Out are separate nodes
        frame = build_scoped(bundle, (), None).passes[0]
        return build_scoped(bundle, frame.marker_path,
                            (frame.first_eid, frame.last_eid))

    def test_self_rw_working_set_is_internal(self):
        fg = self._scoped({
            'M': [(10, 'CS_RWResource')],   # FSR history: rw by one node
            'HDR': [(20, 'ColorTarget')],
        })
        m = [n for n in fg.resources if n.res_key == 'M'][0]
        self.assertEqual(m.reader_ids, [])  # the self-read is dropped
        self.assertTrue(m.internal)

    def test_consumed_output_is_not_internal(self):
        fg = self._scoped({
            'M': [(10, 'CS_RWResource'), (20, 'PS_Resource')],
            'HDR': [(20, 'ColorTarget')],
        })
        m = [n for n in fg.resources if n.res_key == 'M'][0]
        self.assertFalse(m.internal)

    def test_write_only_with_outside_reader_is_not_internal(self):
        leaves = [
            dispatch(10, markers=('Frame', 'Camera', 'FSR')),
            draw(20, ('HDR',), markers=('Frame', 'Late')),
        ]
        usage = {
            'M': [(10, 'CS_RWResource'), (20, 'PS_Resource')],
            'HDR': [(20, 'ColorTarget')],
        }
        bundle = _bundle(leaves, usage)
        frame = build_scoped(bundle, (), None).passes[0]
        lvl2 = build_scoped(bundle, frame.marker_path,
                            (frame.first_eid, frame.last_eid))
        cam = [p for p in lvl2.passes if p.name == 'Camera'][0]
        fg = build_scoped(bundle, cam.marker_path,
                          (cam.first_eid, cam.last_eid))
        m = [n for n in fg.resources if n.res_key == 'M'][0]
        self.assertEqual(m.outside_readers, 1)
        self.assertFalse(m.internal)  # consumed elsewhere in the frame

    def test_swapchain_presented_outside_is_not_internal(self):
        # Present is normalized into usage_by_res during extraction; direct
        # scoped tests model that read explicitly.
        from tests.fakes import present
        leaves = [
            draw(10, ('SC',), markers=('Frame', 'Blit')),
            present(20, src='SC'),
        ]
        usage = {
            'SC': [(10, 'ColorTarget'), (20, 'Present')],
        }
        bundle = _bundle(leaves, usage)
        frame = build_scoped(bundle, (), None).passes[0]
        fg = build_scoped(bundle, frame.marker_path,
                          (frame.first_eid, frame.last_eid))
        sc = [n for n in fg.resources if n.res_key == 'SC'][0]
        self.assertEqual(sc.outside_readers, 1)
        self.assertFalse(sc.internal)

    def test_swapchain_kind_never_internal(self):
        # belt and braces for captures without a Present action
        leaves = [
            dispatch(10, markers=('Frame', 'FSR')),
            draw(20, ('SC',), markers=('Frame', 'Out')),
        ]
        usage = {
            'SC': [(20, 'ColorTarget')],
        }
        bundle = _bundle(leaves, usage)
        frame = build_scoped(bundle, (), None).passes[0]
        fg = build_scoped(bundle, frame.marker_path,
                          (frame.first_eid, frame.last_eid))
        sc = [n for n in fg.resources if n.res_key == 'SC'][0]
        self.assertFalse(sc.internal)  # res_kind == 'swapchain'

    def test_imported_single_reader_is_not_internal(self):
        fg = self._scoped({
            'M': [(20, 'PS_Resource')],
            'HDR': [(20, 'ColorTarget')],
        })
        m = [n for n in fg.resources if n.res_key == 'M'][0]
        self.assertTrue(m.imported)
        self.assertFalse(m.internal)  # external-input rule owns this case


class TestScopeInputs(unittest.TestCase):
    def test_resource_written_outside_is_scope_input(self):
        leaves = [
            draw(10, ('SM',), markers=('Frame', 'Shadow')),
            draw(20, ('HDR',), markers=('Frame', 'Camera', 'Lighting')),
        ]
        usage = {
            'SM': [(10, 'ColorTarget'), (20, 'PS_Resource')],
            'HDR': [(20, 'ColorTarget')],
        }
        bundle = _bundle(leaves, usage)
        frame = build_scoped(bundle, (), None).passes[0]
        lvl2 = build_scoped(bundle, frame.marker_path,
                            (frame.first_eid, frame.last_eid))
        cam = [p for p in lvl2.passes if p.name == 'Camera'][0]
        fg = build_scoped(bundle, cam.marker_path,
                          (cam.first_eid, cam.last_eid))
        sm = [n for n in fg.resources if n.res_key == 'SM'][0]
        self.assertTrue(sm.imported)      # no writer inside the scope
        self.assertTrue(sm.scope_input)   # but written elsewhere in frame
        self.assertEqual(sm.outside_writers, 1)


class TestSiblingReaderPortals(unittest.TestCase):
    """Consumer portals exist only for resources THIS scope wrote:
    other external readers of a pure input are siblings with no causal
    link to the scope - their portals are noise. Producer portals stay."""

    def _bundle(self):
        # GB written by Prep; Camera and UI both only READ it.
        # HDR is written inside Camera and read by Post outside.
        leaves = [
            draw(10, ('GB',), markers=('Frame', 'Prep', 'Fill')),
            draw(20, ('HDR',), markers=('Frame', 'Camera', 'Lit')),
            draw(30, ('SM',), markers=('Frame', 'UI', 'Overlay')),
            draw(40, ('SM',), markers=('Frame', 'Post', 'Blit')),
        ]
        usage = {
            'GB': [(10, 'ColorTarget'), (20, 'PS_Resource'),
                   (30, 'PS_Resource')],
            'HDR': [(20, 'ColorTarget'), (40, 'PS_Resource')],
        }
        return _bundle(leaves, usage)

    def _camera_view(self, bundle):
        frame = build_scoped(bundle, (), None).passes[0]
        lvl2 = build_scoped(bundle, frame.marker_path,
                            (frame.first_eid, frame.last_eid))
        cam = [p for p in lvl2.passes if p.name == 'Camera'][0]
        return build_scoped(bundle, cam.marker_path,
                            (cam.first_eid, cam.last_eid))

    def test_sibling_reader_of_pure_input_gets_no_portal(self):
        fg = self._camera_view(self._bundle())
        portals = [p for p in fg.passes if p.kind == 'portal']
        # pure input keeps producer portal only
        producer_paths = [p.portal_path for p in portals
                          if p.portal_role == 'producer']
        consumer_paths = [p.portal_path for p in portals
                          if p.portal_role == 'consumer']
        self.assertIn(('Frame', 'Prep'), producer_paths)
        self.assertNotIn(('Frame', 'UI'), consumer_paths)

    def test_written_resource_keeps_consumer_portal(self):
        fg = self._camera_view(self._bundle())
        consumer_paths = [p.portal_path for p in fg.passes
                          if p.kind == 'portal' and
                          p.portal_role == 'consumer']
        # HDR was written inside Camera; Post reads it downstream
        self.assertIn(('Frame', 'Post'), consumer_paths)

    def test_tooltip_counts_survive_the_filter(self):
        fg = self._camera_view(self._bundle())
        gb = [n for n in fg.resources if n.res_key == 'GB'][0]
        self.assertEqual(gb.outside_readers, 1)  # UI still counted
        self.assertEqual(gb.outside_writers, 1)

    def test_external_rw_on_pure_input_keeps_producer_only(self):
        # an external compute does RW on a resource Camera only reads:
        # its producer half stays (it may be the input's source), the
        # consumer half is filtered
        leaves = [
            dispatch(10, markers=('Frame', 'Sim')),
            draw(20, ('HDR',), markers=('Frame', 'Camera', 'Lit')),
        ]
        usage = {
            'GB': [(10, 'CS_RWResource'), (20, 'PS_Resource')],
            'HDR': [(20, 'ColorTarget')],
        }
        bundle = _bundle(leaves, usage)
        fg = self._camera_view(bundle)
        roles = [(p.portal_path, p.portal_role) for p in fg.passes
                 if p.kind == 'portal']
        self.assertIn((('Frame', 'Sim'), 'producer'), roles)
        self.assertNotIn((('Frame', 'Sim'), 'consumer'), roles)


class TestNodePortals(unittest.TestCase):
    """Node portal tests for parent-level external leaves."""

    def _bundle(self):
        # Camera writes HDR; a bare leaf directly under Frame consumes it
        leaves = [
            draw(10, ('GB',), markers=('Frame', 'Camera', 'Gbuffer')),
            draw(20, ('HDR',), markers=('Frame', 'Camera', 'Post')),
            draw(30, ('SC',), markers=('Frame',)),  # no deeper marker
        ]
        usage = {
            'GB': [(10, 'ColorTarget')],
            'HDR': [(20, 'ColorTarget'), (30, 'PS_Resource')],
            'SC': [(30, 'ColorTarget')],
        }
        return _bundle(leaves, usage)

    def _camera_view(self, bundle):
        frame = build_scoped(bundle, (), None).passes[0]
        lvl2 = build_scoped(bundle, frame.marker_path,
                            (frame.first_eid, frame.last_eid))
        cam = [p for p in lvl2.passes if p.name == 'Camera'][0]
        return build_scoped(bundle, cam.marker_path,
                            (cam.first_eid, cam.last_eid))

    def test_bare_parent_leaf_becomes_node_portal(self):
        bundle = self._bundle()
        fg = self._camera_view(bundle)
        portals = [p for p in fg.passes if p.kind == 'portal']
        self.assertEqual(len(portals), 1)
        portal = portals[0]
        self.assertNotEqual(portal.name, 'Frame')  # node portal target
        self.assertEqual(portal.portal_path, ('Frame',))
        self.assertEqual(portal.portal_focus_eid, 30)
        self.assertEqual(portal.portal_role, 'consumer')

    def test_node_portal_name_matches_parent_view(self):
        # label matches the parent-view target
        bundle = self._bundle()
        portal = [p for p in self._camera_view(bundle).passes
                  if p.kind == 'portal'][0]
        frame = build_scoped(bundle, (), None).passes[0]
        parent = build_scoped(bundle, frame.marker_path,
                              (frame.first_eid, frame.last_eid))
        target = [p for p in parent.passes
                  if p.kind != 'portal' and
                  p.first_eid <= 30 <= p.last_eid][0]
        self.assertEqual(portal.name, target.name)

    def test_portal_is_the_parents_bundled_node(self):
        # three bare consecutive copies under the parent write what
        # Camera reads: with bundling on, the PARENT view merges them
        # into one x3 node, and the portal IS that node - same name,
        # same member rows, same focus (merging happens once in
        # the parse layer, portals just mirror it)
        from tests.fakes import transfer
        leaves = [
            transfer(10, dst='GB', name='vkCmdCopyBuffer()'),
            transfer(12, dst='GB', name='vkCmdCopyBuffer() #2'),
            transfer(14, dst='GB', name='vkCmdCopyBuffer() #3'),
            draw(20, ('HDR',), markers=('Frame', 'Camera', 'Lit')),
        ]
        usage = {
            'GB': [(10, 'CopyDst'), (12, 'CopyDst'), (14, 'CopyDst'),
                   (20, 'PS_Resource')],
            'HDR': [(20, 'ColorTarget')],
        }
        bundle = _bundle(leaves, usage)
        fg = build_scoped(bundle, ('Frame', 'Camera'), (20, 20),
                          bundling=True)
        portals = [p for p in fg.passes if p.kind == 'portal']
        self.assertEqual(len(portals), 1)
        portal = portals[0]
        self.assertIn(u'×3', portal.name)
        self.assertEqual(len(portal.bundle_members), 3)
        self.assertEqual(len(portal.bundle_member_eids), 3)
        self.assertEqual(portal.portal_role, 'producer')
        # portal mirrors the jump target
        parent = build_scoped(bundle, (), (10, 20), bundling=True,
                              make_portals=False)
        target = [p for p in parent.passes
                  if p.first_eid <= portal.portal_focus_eid
                  <= p.last_eid][0]
        self.assertEqual(portal.name, target.name)
        self.assertEqual(portal.bundle_members, target.bundle_members)

    def test_unbundled_view_mirrors_unbundled_parent(self):
        # bundling off: the parent shows three separate copies, so the
        # scope shows three separate portals - consistent either way
        from tests.fakes import transfer
        leaves = [
            transfer(10, dst='GB', name='vkCmdCopyBuffer()'),
            transfer(12, dst='GB', name='vkCmdCopyBuffer() #2'),
            transfer(14, dst='GB', name='vkCmdCopyBuffer() #3'),
            draw(20, ('HDR',), markers=('Frame', 'Camera', 'Lit')),
        ]
        usage = {
            'GB': [(10, 'CopyDst'), (12, 'CopyDst'), (14, 'CopyDst'),
                   (20, 'PS_Resource')],
            'HDR': [(20, 'ColorTarget')],
        }
        bundle = _bundle(leaves, usage)
        fg = build_scoped(bundle, ('Frame', 'Camera'), (20, 20))
        portals = [p for p in fg.passes if p.kind == 'portal']
        self.assertEqual(len(portals), 3)

    def test_portals_for_different_resources_stay_apart(self):
        # copies writing GB and copies writing SM are different
        # behaviours: the parent view bundles them as two x3 nodes, and
        # the portals mirror that - never one big mixed portal
        from tests.fakes import transfer
        leaves = [
            transfer(10, dst='GB', name='vkCmdCopyBuffer()'),
            transfer(11, dst='GB', name='vkCmdCopyBuffer() #2'),
            transfer(12, dst='GB', name='vkCmdCopyBuffer() #3'),
            transfer(14, dst='SM', name='vkCmdCopyBuffer() #4'),
            transfer(15, dst='SM', name='vkCmdCopyBuffer() #5'),
            transfer(16, dst='SM', name='vkCmdCopyBuffer() #6'),
            draw(20, ('HDR',), markers=('Frame', 'Camera', 'Lit')),
        ]
        usage = {
            'GB': [(10, 'CopyDst'), (11, 'CopyDst'), (12, 'CopyDst'),
                   (20, 'PS_Resource')],
            'SM': [(14, 'CopyDst'), (15, 'CopyDst'), (16, 'CopyDst'),
                   (20, 'PS_Resource')],
            'HDR': [(20, 'ColorTarget')],
        }
        bundle = _bundle(leaves, usage)
        fg = build_scoped(bundle, ('Frame', 'Camera'), (20, 20),
                          bundling=True)
        portals = [p for p in fg.passes if p.kind == 'portal']
        self.assertEqual(len(portals), 2)
        res_by_id = dict((n.id, n) for n in fg.resources)
        for portal in portals:
            self.assertIn(u'×3', portal.name)
            written = set()
            for e in fg.edges:
                if e.src_id == portal.id and e.kind == 'write':
                    written.add(res_by_id[e.dst_id].res_key)
            self.assertEqual(len(written), 1)  # one resource per portal

    def test_unbundled_parent_nodes_mean_unbundled_portals(self):
        # three copies each write ONE member of a name-similar family:
        # in the parent view their write sets DIFFER (episode-exact
        # rule), so the parent keeps three separate copies - and the
        # portals mirror that exactly instead of inventing a merge the
        # jump target does not show
        from tests.fakes import transfer
        leaves = [
            transfer(10, dst='Buf_0', name='vkCmdCopyBuffer()'),
            transfer(12, dst='Buf_1', name='vkCmdCopyBuffer() #2'),
            transfer(14, dst='Buf_2', name='vkCmdCopyBuffer() #3'),
            draw(20, ('HDR',), markers=('Frame', 'Camera', 'Lit')),
        ]
        usage = {
            'Buf_0': [(10, 'CopyDst'), (20, 'PS_Resource')],
            'Buf_1': [(12, 'CopyDst'), (20, 'PS_Resource')],
            'Buf_2': [(14, 'CopyDst'), (20, 'PS_Resource')],
            'HDR': [(20, 'ColorTarget')],
        }
        bundle = _bundle(leaves, usage)
        bundle = dict(bundle)
        bundle['res_info'] = dict(bundle['res_info'])
        bundle['res_names'] = dict(bundle['res_names'])
        for k in ('Buf_0', 'Buf_1', 'Buf_2'):
            bundle['res_info'][k] = {'kind': 'buffer', 'info': {}}
            bundle['res_names'][k] = k
        fg = build_scoped(bundle, ('Frame', 'Camera'), (20, 20),
                          bundling=True)
        portals = [p for p in fg.passes if p.kind == 'portal']
        self.assertEqual(len(portals), 3)
        parent = build_scoped(bundle, (), (10, 20), bundling=True,
                              make_portals=False)
        parent_names = set(p.name for p in parent.passes)
        for portal in portals:
            self.assertIn(portal.name, parent_names)

    def test_dissimilar_node_portals_stay_apart(self):
        from tests.fakes import transfer
        leaves = [
            transfer(10, dst='GB', name='vkCmdCopyBuffer()'),
            transfer(12, dst='GB', name='UploadStaging'),
            draw(20, ('HDR',), markers=('Frame', 'Camera', 'Lit')),
        ]
        usage = {
            'GB': [(10, 'CopyDst'), (12, 'CopyDst'),
                   (20, 'PS_Resource')],
            'HDR': [(20, 'ColorTarget')],
        }
        bundle = _bundle(leaves, usage)
        fg = build_scoped(bundle, ('Frame', 'Camera'), (20, 20))
        portals = [p for p in fg.passes if p.kind == 'portal']
        self.assertEqual(len(portals), 2)

    def test_markerless_present_becomes_root_node_portal(self):
        from tests.fakes import present
        leaves = [
            draw(10, ('SC',), markers=('Frame', 'Blit')),
            present(20, src='SC'),
        ]
        usage = {'SC': [(10, 'ColorTarget'), (20, 'Present')]}
        bundle = _bundle(leaves, usage)
        frame = build_scoped(bundle, (), None).passes[0]
        fg = build_scoped(bundle, frame.marker_path,
                          (frame.first_eid, frame.last_eid))
        portals = [p for p in fg.passes if p.kind == 'portal']
        self.assertEqual(len(portals), 1)
        self.assertEqual(portals[0].portal_path, ())  # whole-frame root
        self.assertEqual(portals[0].portal_focus_eid, 20)

    def test_sibling_scope_portal_unchanged(self):
        # non-degenerate case: toucher inside a sibling marker keeps the
        # scope portal (common prefix + 1) with no focus hint
        leaves = [
            draw(10, ('HDR',), markers=('Frame', 'Camera', 'Post')),
            draw(20, ('GB',), markers=('Frame', 'UI', 'Overlay')),
        ]
        usage = {
            'HDR': [(10, 'ColorTarget'), (20, 'PS_Resource')],
            'GB': [(20, 'ColorTarget')],
        }
        bundle = _bundle(leaves, usage)
        frame = build_scoped(bundle, (), None).passes[0]
        lvl2 = build_scoped(bundle, frame.marker_path,
                            (frame.first_eid, frame.last_eid))
        cam = [p for p in lvl2.passes if p.name == 'Camera'][0]
        fg = build_scoped(bundle, cam.marker_path,
                          (cam.first_eid, cam.last_eid))
        portals = [p for p in fg.passes if p.kind == 'portal']
        self.assertEqual(len(portals), 1)
        self.assertEqual(portals[0].portal_path, ('Frame', 'UI'))
        self.assertIsNone(portals[0].portal_focus_eid)


class TestLeafRuns(unittest.TestCase):
    """_leaf_runs: contiguous (first_eid, last_eid) ranges of leaves whose
    marker path starts with a prefix; a non-matching leaf breaks the run."""

    def test_two_instances_split_on_non_matching_leaf(self):
        leaves = [dispatch(1, markers=('A',)),
                  dispatch(2, markers=('A', 'X')),
                  dispatch(3, markers=('B',)),         # breaks the A run
                  dispatch(4, markers=('A',)),
                  dispatch(5, markers=('A', 'Y'))]
        self.assertEqual(_leaf_runs(leaves, ('A',)), [(1, 2), (4, 5)])

    def test_single_contiguous_run(self):
        leaves = [dispatch(7, markers=('A',)), dispatch(9, markers=('A', 'X'))]
        self.assertEqual(_leaf_runs(leaves, ('A',)), [(7, 9)])

    def test_empty_prefix_spans_all_leaves(self):
        leaves = [dispatch(1, markers=('A',)), dispatch(2, markers=('B',)),
                  dispatch(3, markers=('C',))]
        self.assertEqual(_leaf_runs(leaves, ()), [(1, 3)])

    def test_no_match_yields_no_runs(self):
        leaves = [dispatch(1, markers=('A',)), dispatch(2, markers=('B',))]
        self.assertEqual(_leaf_runs(leaves, ('Z',)), [])


class TestRunContaining(unittest.TestCase):
    """_run_containing: the run holding eid, else the nearest run by endpoint
    distance, else None for no runs."""

    def test_returns_containing_run(self):
        self.assertEqual(_run_containing([(1, 2), (4, 5)], 2), (1, 2))
        self.assertEqual(_run_containing([(1, 2), (4, 5)], 4), (4, 5))

    def test_before_all_runs_picks_first(self):
        self.assertEqual(_run_containing([(4, 5), (8, 9)], 0), (4, 5))

    def test_after_all_runs_picks_last(self):
        self.assertEqual(_run_containing([(1, 2), (8, 9)], 20), (8, 9))

    def test_gap_picks_nearer_run(self):
        # eid 7 sits in the gap, one step from (8,9) and four from (1,2)
        self.assertEqual(_run_containing([(1, 2), (8, 9)], 7), (8, 9))

    def test_no_runs_returns_none(self):
        self.assertIsNone(_run_containing([], 5))


if __name__ == '__main__':
    unittest.main()
