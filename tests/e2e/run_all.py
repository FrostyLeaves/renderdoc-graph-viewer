# -*- coding: utf-8 -*-
r"""Host driver for generating, capturing and verifying e2e scenes.

  python run_all.py            # all scenes, all APIs
  python run_all.py compute_chain unused   # a subset of scenes
"""
import glob
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, '..', '..'))
BUILD = os.path.join(HERE, '_build')
SCENES = os.path.join(HERE, 'scenes')
def _qrenderdoc():
    """QRENDERDOC override, then PATH, then under %ProgramFiles%\\RenderDoc,
    else the bare name (resolved on PATH at run time). No fixed path baked in."""
    p = os.environ.get('QRENDERDOC') or shutil.which('qrenderdoc.exe')
    if p:
        return p
    pf = os.environ.get('ProgramFiles', '')
    hits = glob.glob(os.path.join(pf, 'RenderDoc', 'qrenderdoc.exe')) if pf else []
    return hits[0] if hits else 'qrenderdoc.exe'


QRD = _qrenderdoc()
REPORT = os.path.join(REPO, 'tools', '_probe_out', 'verify_all.txt')

APIS = ['vulkan', 'd3d11', 'd3d12']
EXE = {'vulkan': 'generic_vk.exe', 'd3d11': 'generic_d3d11.exe',
       'd3d12': 'generic_d3d12.exe'}

sys.path.insert(0, HERE)
import gen_all


def scenes_to_run(argv):
    if argv:
        return [os.path.join(SCENES, s if s.endswith('.yaml') else s + '.yaml')
                for s in argv]
    return sorted(glob.glob(os.path.join(SCENES, '*.yaml')))


def main():
    scene_files = scenes_to_run(sys.argv[1:])
    for sf in scene_files:
        for api in APIS:
            out_dir = gen_all.gen_scene(sf, api, BUILD)
            exe = os.path.join(BUILD, EXE[api])
            if not os.path.exists(exe):
                print('MISSING exe %s -- run build_runtime.bat' % exe)
                continue
            # clear stale captures so the verifier picks this run's
            for old in glob.glob(os.path.join(out_dir, '*.rdc')):
                os.remove(old)
            cap = os.path.join(out_dir, 'cap')
            r = subprocess.run([exe, os.path.join(out_dir, 'manifest.json'),
                                out_dir, cap],
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            print('%-14s %-7s %s' % (os.path.basename(sf), api,
                  'captured' if r.returncode == 0 else 'EXE FAIL'))
            if r.returncode != 0:
                print(r.stdout.decode('utf-8', 'replace'))

    print('--- verifying in qrenderdoc ---')
    env = dict(os.environ)
    env['DEMO_BUILD'] = BUILD
    subprocess.run([QRD, '--python', os.path.join(HERE, 'verify_all.py')],
                   env=env)
    if os.path.exists(REPORT):
        with open(REPORT, 'r', encoding='utf-8') as f:
            print(f.read())


if __name__ == '__main__':
    main()
