# -*- coding: utf-8 -*-
r"""E2E qrenderdoc entry for generated captures under DEMO_BUILD.

  python run_all.py                                  # does gen + capture + this
  qrenderdoc.exe --python tests\e2e\verify_all.py   # verify existing captures
"""
import glob
import json
import os
import sys
import traceback

GPU = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(GPU, '..', '..'))
for p in (REPO, GPU):
    if p not in sys.path:
        sys.path.insert(0, p)
OUT = os.path.join(REPO, 'tools', '_probe_out', 'verify_all.txt')
os.makedirs(os.path.dirname(OUT), exist_ok=True)
_lf = open(OUT, 'w', encoding='utf-8')


def P(*a):
    _lf.write(' '.join(str(x) for x in a) + '\n')
    _lf.flush()


def main():
    import renderdoc as rd
    import verifylib

    build = os.environ.get('DEMO_BUILD', os.path.join(GPU, '_build'))
    # build/<scene>/<api>/{manifest,oracle,cap*.rdc}
    dirs = sorted(glob.glob(os.path.join(build, '*', '*')))
    overall = True
    total = 0
    for d in dirs:
        exp_path = os.path.join(d, 'expected.json')
        if not os.path.isfile(exp_path):
            continue
        scene = os.path.basename(os.path.dirname(d))
        api = os.path.basename(d)
        cap = verifylib.newest_capture(d)
        if not cap:
            P('%-14s %-7s NO CAPTURE' % (scene, api))
            overall = False
            continue
        with open(exp_path, 'r', encoding='utf-8') as f:
            expected = json.load(f)
        P('%s / %s' % (scene, api))
        try:
            capf = rd.OpenCaptureFile()
            capf.OpenFile(cap, '', None)
            r, ctrl = capf.OpenCapture(rd.ReplayOptions(), None)
            try:
                ok, n = verifylib.verify_capture(ctrl, rd, expected, P)
            finally:
                ctrl.Shutdown()
                capf.Shutdown()
            total += 1
            if not ok:
                overall = False
        except Exception:
            P('  EXC\n' + traceback.format_exc())
            overall = False
    P('\nOVERALL (%d captures): %s' % (total, 'PASS' if overall else 'FAIL'))


try:
    main()
except Exception:
    P('EXC\n' + traceback.format_exc())
finally:
    _lf.close()
    os._exit(0)
