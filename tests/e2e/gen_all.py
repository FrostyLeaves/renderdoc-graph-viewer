# -*- coding: utf-8 -*-
"""Host-side generation for one scene and API."""
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, '..', '..'))
for _p in (_REPO, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from gen import schema
from gen import manifest as mf
from gen import compile as cc
import refmodel as rm


def gen_scene(scene_path, api, build_root):
    scene = schema.load_scene(scene_path)
    out_dir = os.path.join(build_root, scene.name, api)
    src_dir = os.path.join(out_dir, 'hlsl')
    os.makedirs(out_dir, exist_ok=True)

    cc.compile_scene(scene, api, src_dir, out_dir)

    with open(os.path.join(out_dir, 'manifest.json'), 'w', encoding='utf-8') as f:
        json.dump(mf.emit_manifest(scene), f, indent=1)

    expected = {'_bundling': scene.bundling}
    instances = rm.scope_instances(scene, api=api)
    levels = {}
    for instkey, path, rng in instances:
        nodes, edges = rm.expected_instance(scene, path, rng,
                                            bundling=scene.bundling, api=api)
        levels[instkey] = {'nodes': sorted(nodes), 'edges': sorted(edges)}
    expected['refined'] = levels
    mn, me = rm.expected_merged(scene, api=api)
    expected['merged'] = {'nodes': sorted(mn), 'edges': sorted(me)}
    with open(os.path.join(out_dir, 'expected.json'), 'w', encoding='utf-8') as f:
        json.dump(expected, f, indent=1)

    print('generated %s (%s) -> %s' % (scene.name, api, out_dir))
    return out_dir


if __name__ == '__main__':
    if len(sys.argv) < 4:
        print('usage: gen_all.py <scene.yaml> <api> <build_root>')
        sys.exit(1)
    gen_scene(sys.argv[1], sys.argv[2], sys.argv[3])
