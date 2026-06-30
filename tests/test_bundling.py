# -*- coding: utf-8 -*-
"""Behavior-equivalent resource bundling: resources whose edge
structure is IDENTICAL (same writers, same readers, same kind - not name
similarity) merge into one visual node behind a toggle."""

import unittest

from renderdoc_graph_viewer.graph_model import (build_passes, build_graph,
                                             bundle_equivalent_passes,
                                             bundle_equivalent_resources,
                                             _name_signature)
from tests.fakes import draw, dispatch, transfer

RES_INFO = {
    'Mesh_VertexBuffer': {'kind': 'buffer', 'info': {}},
    'Mesh_SkinMatrixBuffer': {'kind': 'buffer', 'info': {}},
    'Mesh_PropertyBuffer': {'kind': 'buffer', 'info': {}},
    'Mesh BakeMeshMatrixBuffer': {'kind': 'buffer', 'info': {}},
    'GridBuffer': {'kind': 'buffer', 'info': {}},
    'VolumeBuffer': {'kind': 'buffer', 'info': {}},
    'Filter_Work0': {'kind': 'buffer', 'info': {}},
    'Filter_Work1': {'kind': 'buffer', 'info': {}},
    'Filter_Work2': {'kind': 'buffer', 'info': {}},
    'Filter_Work3': {'kind': 'buffer', 'info': {}},
    'Blur_Mip0': {'kind': 'uav_tex', 'info': {}},
    'Blur_Mip1': {'kind': 'uav_tex', 'info': {}},
    'Blur_Mip2': {'kind': 'uav_tex', 'info': {}},
    'OtherTex': {'kind': 'color', 'info': {}},
    'HDR': {'kind': 'color', 'info': {}},
}


def _fg(usage):
    passes = build_passes([
        dispatch(10, markers=('Skin',)),
        draw(20, ('HDR',), markers=('Draw',)),
    ])
    fg = build_graph(passes, usage, RES_INFO, versioned=True)
    bundle_equivalent_resources(fg)
    return fg


class TestBundling(unittest.TestCase):
    def test_identical_behavior_buffers_merge(self):
        fg = _fg({
            'Mesh_VertexBuffer': [(10, 'CS_RWResource'), (20, 'VS_Resource')],
            'Mesh_SkinMatrixBuffer': [(10, 'CS_RWResource'), (20, 'VS_Resource')],
            'Mesh_PropertyBuffer': [(10, 'CS_RWResource'), (20, 'VS_Resource')],
            'HDR': [(20, 'ColorTarget')],
        })
        buffers = [n for n in fg.resources if n.res_kind == 'buffer']
        self.assertEqual(len(buffers), 1)
        bundle = buffers[0]
        self.assertIn(u'×3', bundle.name)
        self.assertTrue(bundle.name.startswith('Mesh_'))
        self.assertEqual(len(bundle.bundle_members), 3)
        self.assertIn('Mesh_VertexBuffer', bundle.bundle_members)

    def test_bundle_edges_are_deduplicated(self):
        fg = _fg({
            'Mesh_VertexBuffer': [(10, 'CS_RWResource'), (20, 'VS_Resource')],
            'Mesh_SkinMatrixBuffer': [(10, 'CS_RWResource'), (20, 'VS_Resource')],
            'Mesh_PropertyBuffer': [(10, 'CS_RWResource'), (20, 'VS_Resource')],
        })
        bundle = [n for n in fg.resources if n.res_kind == 'buffer'][0]
        touching = [e for e in fg.edges
                    if bundle.id in (e.src_id, e.dst_id)]
        kinds = sorted(e.kind for e in touching)
        self.assertEqual(kinds, ['read', 'write'])
        read_edge = [e for e in touching if e.kind == 'read'][0]
        self.assertEqual(len(read_edge.usages), 3)  # merged from all members

    def test_two_members_stay_below_threshold(self):
        # tiny groups (e.g. just 2) are not worth bundling
        fg = _fg({
            'Mesh_VertexBuffer': [(10, 'CS_RWResource'), (20, 'VS_Resource')],
            'Mesh_SkinMatrixBuffer': [(10, 'CS_RWResource'), (20, 'VS_Resource')],
        })
        buffers = [n for n in fg.resources if n.res_kind == 'buffer']
        self.assertEqual(len(buffers), 2)

    def test_bundle_carries_member_keys_for_navigation(self):
        fg = _fg({
            'Mesh_VertexBuffer': [(10, 'CS_RWResource'), (20, 'VS_Resource')],
            'Mesh_SkinMatrixBuffer': [(10, 'CS_RWResource'), (20, 'VS_Resource')],
            'Mesh_PropertyBuffer': [(10, 'CS_RWResource'), (20, 'VS_Resource')],
        })
        bundle = [n for n in fg.resources if n.res_kind == 'buffer'][0]
        self.assertEqual(len(bundle.bundle_member_keys),
                         len(bundle.bundle_members))
        self.assertIn('Mesh_VertexBuffer', bundle.bundle_member_keys)

    def test_different_readers_do_not_merge(self):
        fg = _fg({
            'Mesh_VertexBuffer': [(10, 'CS_RWResource'), (20, 'VS_Resource')],
            'Mesh_SkinMatrixBuffer': [(10, 'CS_RWResource')],  # nobody reads
        })
        buffers = [n for n in fg.resources if n.res_kind == 'buffer']
        self.assertEqual(len(buffers), 2)

    def test_different_kind_does_not_merge(self):
        fg = _fg({
            'Mesh_VertexBuffer': [(10, 'CS_RWResource'), (20, 'VS_Resource')],
            'OtherTex': [(10, 'CS_RWResource'), (20, 'PS_Resource')],
        })
        names = sorted(n.name for n in fg.resources if n.res_key != 'HDR')
        self.assertEqual(len(names), 2)  # buffer and texture stay apart

    def test_rank_edges_remapped_to_live_ids(self):
        fg = _fg({
            'Mesh_VertexBuffer': [(10, 'CS_RWResource'), (20, 'VS_Resource')],
            'Mesh_SkinMatrixBuffer': [(10, 'CS_RWResource'), (20, 'VS_Resource')],
            'HDR': [(20, 'ColorTarget')],
        })
        live = set(p.id for p in fg.passes)
        live.update(n.id for n in fg.resources)
        for e in fg.rank_edges:
            self.assertIn(e.src_id, live)
            self.assertIn(e.dst_id, live)

    def test_digit_variants_bundle_together(self):
        # XXX_Mip0 / XXX_Mip1 / XXX_Mip2: digit runs normalise away
        fg = _fg({
            'Blur_Mip0': [(10, 'CS_RWResource'), (20, 'PS_Resource')],
            'Blur_Mip1': [(10, 'CS_RWResource'), (20, 'PS_Resource')],
            'Blur_Mip2': [(10, 'CS_RWResource'), (20, 'PS_Resource')],
        })
        mips = [n for n in fg.resources if n.res_kind == 'uav_tex']
        self.assertEqual(len(mips), 1)
        self.assertIn(u'×3', mips[0].name)

    def test_identical_behavior_but_unrelated_names_not_bundled(self):
        # behaviour matches but names share no head/tail structure: the
        # name-similarity gate keeps them apart (generic, not name lists)
        fg = _fg({
            'Mesh_VertexBuffer': [(10, 'CS_RWResource'), (20, 'VS_Resource')],
            'GridBuffer': [(10, 'CS_RWResource'), (20, 'VS_Resource')],
            'VolumeBuffer': [(10, 'CS_RWResource'), (20, 'VS_Resource')],
        })
        buffers = [n for n in fg.resources if n.res_kind == 'buffer']
        self.assertEqual(len(buffers), 3)

    def test_mixed_group_bundles_only_the_similar_family(self):
        fg = _fg({
            'Mesh_VertexBuffer': [(10, 'CS_RWResource'), (20, 'VS_Resource')],
            'Mesh_SkinMatrixBuffer': [(10, 'CS_RWResource'), (20, 'VS_Resource')],
            'Mesh BakeMeshMatrixBuffer': [(10, 'CS_RWResource'),
                                            (20, 'VS_Resource')],
            'GridBuffer': [(10, 'CS_RWResource'), (20, 'VS_Resource')],
        })
        buffers = [n for n in fg.resources if n.res_kind == 'buffer']
        self.assertEqual(len(buffers), 2)
        bundle = [n for n in buffers if getattr(n, 'bundle_members', None)][0]
        self.assertEqual(len(bundle.bundle_members), 3)
        single = [n for n in buffers if not getattr(n, 'bundle_members', None)]
        self.assertEqual(single[0].name, 'GridBuffer')

    def test_bundle_inherits_internal_flag(self):
        # bundled self-contained working sets
        fg = _fg({
            'Filter_Work0': [(10, 'CS_RWResource')],
            'Filter_Work1': [(10, 'CS_RWResource')],
            'Filter_Work2': [(10, 'CS_RWResource')],
        })
        bundle = [n for n in fg.resources
                  if getattr(n, 'bundle_members', None)][0]
        self.assertTrue(bundle.internal)

    def test_scope_inputs_with_distinct_outside_writers_stay_apart(self):
        # identical in-scope reads with distinct outside writers
        from renderdoc_graph_viewer.graph_model import build_scoped
        leaves = [
            # outside the Work scope: one copy per buffer
            draw(2, ('OtherTex',), markers=()),
            draw(4, ('OtherTex',), markers=()),
            draw(6, ('OtherTex',), markers=()),
            # the scope: one dispatch reading all three buffers
            dispatch(20, markers=('Work', 'Skin')),
        ]
        bundle = {
            'leaves': leaves,
            'usage_by_res': {
                'Filter_Work0': [(2, 'CopyDst'), (20, 'CS_Resource')],
                'Filter_Work1': [(4, 'CopyDst'), (20, 'CS_Resource')],
                'Filter_Work2': [(6, 'CopyDst'), (20, 'CS_Resource')],
            },
            'res_info': RES_INFO, 'res_names': {}, 'rid_objects': {},
            'warnings': [], 'seconds': 0.0,
        }
        fg = build_scoped(bundle, ('Work',), (20, 20), bundling=True)
        names = sorted(n.name for n in fg.resources
                       if n.name.startswith('Filter'))
        self.assertEqual(
            names, ['Filter_Work0', 'Filter_Work1', 'Filter_Work2'])

    def test_pure_cross_frame_inputs_still_bundle(self):
        # pure external inputs share an empty outside-writer set
        from renderdoc_graph_viewer.graph_model import build_scoped
        leaves = [dispatch(20, markers=('Work', 'Skin'))]
        bundle = {
            'leaves': leaves,
            'usage_by_res': {
                'Filter_Work0': [(20, 'CS_Resource')],
                'Filter_Work1': [(20, 'CS_Resource')],
                'Filter_Work2': [(20, 'CS_Resource')],
            },
            'res_info': RES_INFO, 'res_names': {}, 'rid_objects': {},
            'warnings': [], 'seconds': 0.0,
        }
        fg = build_scoped(bundle, ('Work',), (20, 20), bundling=True)
        bundles = [n for n in fg.resources
                   if getattr(n, 'bundle_members', None)]
        self.assertEqual(len(bundles), 1)
        self.assertEqual(len(bundles[0].bundle_members), 3)

    def test_internal_split_keeps_write_only_resource_separate(self):
        # the _DepthMinMaxTexture vs _DepthPyramid_0 case:
        # folded self-RW working sets and a genuinely write-only
        # sibling share the same edge structure and name family, but
        # bundling them either exposes the working sets or hides the
        # resource that deserves scrutiny - internal is part of the
        # group signature now
        fg = _fg({
            'Filter_Work0': [(10, 'CS_RWResource')],   # folded -> internal
            'Filter_Work1': [(10, 'CS_RWResource')],
            'Filter_Work2': [(10, 'CS_RWResource')],
            'Filter_Work3': [(10, 'CopyDst')],         # write-only, exposed
        })
        bundles = [n for n in fg.resources
                   if getattr(n, 'bundle_members', None)]
        self.assertEqual(len(bundles), 1)
        self.assertEqual(len(bundles[0].bundle_members), 3)
        self.assertTrue(bundles[0].internal)
        loner = [n for n in fg.resources if n.name == 'Filter_Work3']
        self.assertEqual(len(loner), 1)
        self.assertFalse(loner[0].internal)

    def test_singletons_keep_their_identity(self):
        fg = _fg({
            'Mesh_VertexBuffer': [(10, 'CS_RWResource'), (20, 'VS_Resource')],
            'HDR': [(20, 'ColorTarget')],
        })
        names = [n.name for n in fg.resources]
        self.assertIn('Mesh_VertexBuffer', names)
        self.assertIn('HDR', names)


GEN_RES_INFO = {
    'R1': {'kind': 'uav_tex', 'info': {}},
    'R2': {'kind': 'uav_tex', 'info': {}},
    'R3': {'kind': 'uav_tex', 'info': {}},
}


def _generations_fg(rounds):
    # a 3-member family written/read alternately for `rounds` generations
    leaves = []
    usage = {'R1': [], 'R2': [], 'R3': []}
    eid = 10
    for i in range(rounds):
        leaves.append(draw(eid, ('R1', 'R2', 'R3'),
                           markers=('W%d' % i,)))
        for r in ('R1', 'R2', 'R3'):
            usage[r].append((eid, 'ColorTarget'))
        eid += 10
        leaves.append(dispatch(eid, markers=('C%d' % i,)))
        for r in ('R1', 'R2', 'R3'):
            usage[r].append((eid, 'CS_Resource'))
        eid += 10
    passes = build_passes(leaves)
    fg = build_graph(passes, usage, GEN_RES_INFO, versioned=True)
    bundle_equivalent_resources(fg)
    return fg


class TestGenerationCollapse(unittest.TestCase):
    def test_many_generations_collapse_to_one_node(self):
        fg = _generations_fg(rounds=4)
        bundles = [n for n in fg.resources
                   if getattr(n, 'bundle_members', None)]
        self.assertEqual(len(bundles), 1)
        b = bundles[0]
        self.assertEqual(getattr(b, 'generations', 1), 4)
        self.assertIn('4 generations', b.name)
        # all generations' writers and readers hang off the one node
        writes = set(e.src_id for e in fg.edges
                     if e.kind == 'write' and e.dst_id == b.id)
        reads = set(e.dst_id for e in fg.edges
                    if e.kind == 'read' and e.src_id == b.id)
        self.assertEqual(len(writes), 4)
        self.assertEqual(len(reads), 4)

    def test_few_generations_keep_episode_twins(self):
        fg = _generations_fg(rounds=3)
        bundles = sorted((n for n in fg.resources
                          if getattr(n, 'bundle_members', None)),
                         key=lambda n: n.version)
        self.assertEqual(len(bundles), 3)
        self.assertEqual([b.version for b in bundles], [1, 2, 3])


PASS_RES_INFO = {
    'VT': {'kind': 'buffer', 'info': {}},
    'Other': {'kind': 'buffer', 'info': {}},
    'HDR': {'kind': 'color', 'info': {}},
}


def _pass_fg(leaves, usage):
    passes = build_passes(leaves)
    fg = build_graph(passes, usage, PASS_RES_INFO, versioned=True)
    bundle_equivalent_passes(fg)
    return fg


def _vt_leaves():
    # three identical copies write VT; a fourth ALSO writes Other; a
    # reader consumes VT afterwards
    return [
        transfer(10, dst='VT', name='vkCmdCopyBuffer()'),
        transfer(20, dst='VT', name='vkCmdCopyBuffer() #2'),
        transfer(30, dst='VT', name='vkCmdCopyBuffer() #3'),
        transfer(40, dst='VT', name='vkCmdCopyBuffer() #4'),
        draw(50, ('HDR',), markers=('Lit',)),
    ]


def _vt_usage(extra_for_40=False):
    usage = {
        'VT': [(10, 'CopyDst'), (20, 'CopyDst'), (30, 'CopyDst'),
               (40, 'CopyDst'), (50, 'PS_Resource')],
        'HDR': [(50, 'ColorTarget')],
    }
    if extra_for_40:
        usage['Other'] = [(40, 'CopyDst')]
    return usage


class TestCrossEpisodeBundling(unittest.TestCase):
    """Cross-episode pass bundling tests."""

    def test_copies_across_episodes_do_not_bundle(self):
        # one copy per VT episode
        leaves = []
        usage = {'VT': [], 'HDR': []}
        eid = 10
        for i in range(4):
            leaves.append(transfer(eid, dst='VT',
                                   name='vkCmdCopyBuffer() #%d' % (i + 1)))
            usage['VT'].append((eid, 'CopyDst'))
            eid += 10
            leaves.append(draw(eid, ('HDR',), markers=('Lit%d' % i,)))
            usage['VT'].append((eid, 'PS_Resource'))
            usage['HDR'].append((eid, 'ColorTarget'))
            eid += 10
        passes = build_passes(leaves)
        fg = build_graph(passes, usage, PASS_RES_INFO, versioned=True)
        bundle_equivalent_passes(fg)
        copies = [p for p in fg.passes if 'CopyBuffer' in p.name]
        self.assertEqual(len(copies), 4)
        self.assertTrue(all(not getattr(p, 'bundle_members', None)
                            for p in copies))

    def test_interrupted_run_does_not_bundle(self):
        # same behaviour, same episode, but an unrelated pass sits in the
        # middle: 2 + 2 consecutive runs, neither reaches 3 members
        leaves = [
            transfer(10, dst='VT', name='vkCmdCopyBuffer()'),
            transfer(20, dst='VT', name='vkCmdCopyBuffer() #2'),
            dispatch(30, markers=('Sim',)),
            transfer(40, dst='VT', name='vkCmdCopyBuffer() #3'),
            transfer(50, dst='VT', name='vkCmdCopyBuffer() #4'),
            draw(60, ('HDR',), markers=('Lit',)),
        ]
        usage = {
            'VT': [(10, 'CopyDst'), (20, 'CopyDst'), (40, 'CopyDst'),
                   (50, 'CopyDst'), (60, 'PS_Resource')],
            'HDR': [(60, 'ColorTarget')],
        }
        passes = build_passes(leaves)
        fg = build_graph(passes, usage, PASS_RES_INFO, versioned=True)
        bundle_equivalent_passes(fg)
        copies = [p for p in fg.passes if 'CopyBuffer' in p.name]
        self.assertEqual(len(copies), 4)

    def test_consecutive_tail_still_bundles(self):
        # tail run reaches the bundling threshold
        leaves = [
            transfer(10, dst='VT', name='vkCmdCopyBuffer()'),
            transfer(20, dst='VT', name='vkCmdCopyBuffer() #2'),
            dispatch(30, markers=('Sim',)),
            transfer(40, dst='VT', name='vkCmdCopyBuffer() #3'),
            transfer(50, dst='VT', name='vkCmdCopyBuffer() #4'),
            transfer(60, dst='VT', name='vkCmdCopyBuffer() #5'),
            draw(70, ('HDR',), markers=('Lit',)),
        ]
        usage = {
            'VT': [(10, 'CopyDst'), (20, 'CopyDst'), (40, 'CopyDst'),
                   (50, 'CopyDst'), (60, 'CopyDst'), (70, 'PS_Resource')],
            'HDR': [(70, 'ColorTarget')],
        }
        passes = build_passes(leaves)
        fg = build_graph(passes, usage, PASS_RES_INFO, versioned=True)
        bundle_equivalent_passes(fg)
        copies = [p for p in fg.passes if 'CopyBuffer' in p.name]
        bundled = [p for p in copies if getattr(p, 'bundle_members', None)]
        plain = [p for p in copies if not getattr(p, 'bundle_members', None)]
        self.assertEqual(len(bundled), 1)
        self.assertEqual(len(bundled[0].bundle_members), 3)
        self.assertEqual(len(plain), 2)

    def test_family_bundles_share_stable_res_key(self):
        fg = _generations_fg(rounds=3)  # below collapse threshold
        bundles = [n for n in fg.resources
                   if getattr(n, 'bundle_members', None)]
        self.assertEqual(len(bundles), 3)
        self.assertEqual(len(set(b.res_key for b in bundles)), 1)


class TestPassBundling(unittest.TestCase):
    def test_identical_copies_merge(self):
        fg = _pass_fg(_vt_leaves(), _vt_usage())
        copies = [p for p in fg.passes if 'CopyBuffer' in p.name]
        self.assertEqual(len(copies), 1)
        b = copies[0]
        self.assertEqual(len(b.bundle_members), 4)
        self.assertEqual(b.first_eid, 10)
        self.assertEqual(b.last_eid, 40)
        self.assertEqual(b.action_count, 4)
        self.assertIn(u'×4', b.name)

    def test_member_eids_for_row_clicks(self):
        fg = _pass_fg(_vt_leaves(), _vt_usage())
        b = [p for p in fg.passes if 'CopyBuffer' in p.name][0]
        self.assertEqual(b.bundle_member_eids, [10, 20, 30, 40])

    def test_edges_remap_and_dedupe(self):
        fg = _pass_fg(_vt_leaves(), _vt_usage())
        b = [p for p in fg.passes if 'CopyBuffer' in p.name][0]
        writes = [e for e in fg.edges if e.src_id == b.id and
                  e.kind == 'write']
        self.assertEqual(len(writes), 1)
        self.assertEqual([eid for eid, _u in writes[0].usages],
                         [10, 20, 30, 40])

    def test_divergent_behavior_stays_apart(self):
        fg = _pass_fg(_vt_leaves(), _vt_usage(extra_for_40=True))
        copies = [p for p in fg.passes if 'CopyBuffer' in p.name]
        # #4 also writes Other: different edge structure, kept separate
        self.assertEqual(sorted(len(getattr(p, 'bundle_members', []) or [])
                                for p in copies), [0, 3])

    def test_below_min_members_not_merged(self):
        leaves = [
            transfer(10, dst='VT', name='vkCmdCopyBuffer()'),
            transfer(20, dst='VT', name='vkCmdCopyBuffer() #2'),
            draw(50, ('HDR',), markers=('Lit',)),
        ]
        usage = {
            'VT': [(10, 'CopyDst'), (20, 'CopyDst'), (50, 'PS_Resource')],
            'HDR': [(50, 'ColorTarget')],
        }
        fg = _pass_fg(leaves, usage)
        copies = [p for p in fg.passes if 'CopyBuffer' in p.name]
        self.assertEqual(len(copies), 2)

    def test_dissimilar_names_stay_apart(self):
        leaves = [
            transfer(10, dst='VT', name='vkCmdCopyBuffer()'),
            transfer(20, dst='VT', name='vkCmdCopyBuffer() #2'),
            transfer(30, dst='VT', name='UploadStaging'),
            draw(50, ('HDR',), markers=('Lit',)),
        ]
        usage = {
            'VT': [(10, 'CopyDst'), (20, 'CopyDst'), (30, 'CopyDst'),
                   (50, 'PS_Resource')],
            'HDR': [(50, 'ColorTarget')],
        }
        fg = _pass_fg(leaves, usage)
        # name signature differs: no group reaches 3 members
        self.assertEqual(
            [p for p in fg.passes
             if getattr(p, 'bundle_members', None) and 'Copy' in p.name],
            [])

    def test_drillable_aggregates_never_merge(self):
        leaves = [
            draw(10, ('HDR',), markers=('A', 'x')),
            draw(20, ('HDR',), markers=('B', 'x')),
            draw(30, ('HDR',), markers=('C', 'x')),
        ]
        usage = {'HDR': [(10, 'ColorTarget'), (20, 'ColorTarget'),
                         (30, 'ColorTarget')]}
        passes = build_passes(leaves, scope_level=0)
        for p in passes:
            p.drillable = True
        fg = build_graph(passes, usage, PASS_RES_INFO, versioned=True)
        bundle_equivalent_passes(fg)
        self.assertTrue(all(not getattr(p, 'bundle_members', None)
                            for p in fg.passes))


class TestNameSignature(unittest.TestCase):
    """The bundling similarity heuristic: split on separators / camelCase /
    digit boundaries, lowercase, digit runs -> '#', keep (first, last) token."""

    def test_camelcase_splits_to_first_and_last_token(self):
        self.assertEqual(_name_signature('Mesh_VertexBuffer'),
                         ('mesh', 'buffer'))

    def test_separator_and_camelcase_are_equivalent(self):
        # an underscore, a space and a camelCase hump are all token boundaries
        self.assertEqual(_name_signature('Mesh BakeMeshMatrixBuffer'),
                         ('mesh', 'buffer'))

    def test_digit_runs_collapse_to_hash(self):
        self.assertEqual(_name_signature('Blur_Mip0'), ('blur', '#'))
        # any digit run maps to the same '#', regardless of value/width
        self.assertEqual(_name_signature('Blur_Mip0'),
                         _name_signature('Blur_Mip17'))

    def test_infix_token_siblings_share_a_signature(self):
        # the docstring's Mesh_X_Buffer / Mesh_Y_Buffer claim: only first+last
        # survive, so a differing middle token does not split them
        self.assertEqual(_name_signature('Mesh_X_Buffer'),
                         _name_signature('Mesh_Y_Buffer'))

    def test_single_token_returns_one_tuple(self):
        self.assertEqual(_name_signature('HDR'), ('hdr',))

    def test_acronym_then_word_boundary(self):
        # a leading all-caps run is its own token, not glued to the next word
        self.assertEqual(_name_signature('HDRBuffer'), ('hdr', 'buffer'))

    def test_distinct_names_keep_distinct_signatures(self):
        self.assertNotEqual(_name_signature('GridBuffer'),
                            _name_signature('VolumeTexture'))

    def test_punctuation_only_falls_back_to_lowercased_name(self):
        # no alphanumerics to tokenise: the whole (lowercased) name is the key
        self.assertEqual(_name_signature('___'), ('___',))


if __name__ == '__main__':
    unittest.main()
