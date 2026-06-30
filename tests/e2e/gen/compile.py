# -*- coding: utf-8 -*-
"""Write generated HLSL and invoke dxc/fxc to produce the per-API bytecode the
runtimes load. All entry points are named `main`. Python 3.6.

Compilers (override via env):
  DXC  -> dxc.exe   (SPIR-V + DXIL)   default: Vulkan SDK
  FXC  -> fxc.exe   (DXBC)            default: Windows Kit
"""
import os
import subprocess

from . import schema as S
from . import hlsl_codegen as hc

DXC = os.environ.get('DXC', r'C:\VulkanSDK\1.4.313.2\Bin\dxc.exe')
FXC = os.environ.get('FXC',
                     r'C:\Program Files (x86)\Windows Kits\10\bin\10.0.20348.0\x64\fxc.exe')

# api -> (extension, builder(hlsl_path, out_path, entry, target))
EXT = {'vulkan': 'spv', 'd3d12': 'dxil', 'd3d11': 'cso'}


def _run(cmd):
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if r.returncode != 0:
        raise RuntimeError('compile failed: %s\n%s'
                           % (' '.join(cmd), r.stdout.decode('utf-8', 'replace')))


def _compile_one(api, hlsl_path, out_path, target):
    if api == 'vulkan':
        _run([DXC, '-spirv', '-T', target, '-E', 'main', hlsl_path, '-Fo', out_path])
    elif api == 'd3d12':
        _run([DXC, '-T', target, '-E', 'main', hlsl_path, '-Fo', out_path])
    elif api == 'd3d11':
        # fxc uses 5_0 targets
        t5 = target.split('_')[0] + '_5_0'
        _run([FXC, '/nologo', '/T', t5, '/E', 'main', hlsl_path, '/Fo', out_path])
    else:
        raise ValueError('unknown api %r' % api)


def compile_scene(scene, api, src_dir, out_dir):
    """Generate HLSL for each pass, compile for one API. Returns {pass_name: stem}.
    Graphics VS/PS added in Phase 2; compute only here."""
    os.makedirs(src_dir, exist_ok=True)
    os.makedirs(out_dir, exist_ok=True)
    ext = EXT[api]
    stems = {}

    def write_compile(stem, src, target):
        hpath = os.path.join(src_dir, stem + '.hlsl')
        with open(hpath, 'w', encoding='utf-8') as f:
            f.write(src)
        _compile_one(api, hpath, os.path.join(out_dir, stem + '.' + ext), target)

    for p in scene.passes:
        if p.type == S.PASS_COMPUTE:
            write_compile(p.name, hc.gen_compute(p), 'cs_6_0')
            stems[p.name] = p.name
        elif p.type == S.PASS_GRAPHICS:
            vs, ps = hc.gen_graphics(p)
            write_compile(p.name + '_vs', vs, 'vs_6_0')
            write_compile(p.name + '_ps', ps, 'ps_6_0')
            stems[p.name] = p.name
    return stems
