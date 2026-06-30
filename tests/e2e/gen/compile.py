# -*- coding: utf-8 -*-
"""Write generated HLSL and invoke dxc/fxc to produce the per-API bytecode the
runtimes load. All entry points are named `main`. Python 3.6.

Compilers are located by: the DXC / FXC env override, then PATH, then the
newest installed Vulkan SDK / Windows Kit.
  DXC  -> dxc.exe   (SPIR-V + DXIL)
  FXC  -> fxc.exe   (DXBC)
"""
import glob
import os
import shutil
import subprocess

from . import schema as S
from . import hlsl_codegen as hc

def _find(env_var, exe, *globs):
    """Locate a build tool. Order: the env_var override, then PATH, then the
    newest match of each (env-rooted) glob, else the bare exe name (resolved on
    PATH when run). No fixed install path is baked in -- set env_var if detection
    misses."""
    override = os.environ.get(env_var)
    if override:
        return override
    on_path = shutil.which(exe)
    if on_path:
        return on_path
    for pattern in globs:
        hits = sorted(glob.glob(pattern)) if pattern else []
        if hits:
            return hits[-1]   # highest version sorts last
    return exe


# Search roots come from the installers' own env vars, not fixed paths:
# the Vulkan SDK sets VULKAN_SDK; the Windows SDK lives under ProgramFiles(x86).
_VK = os.environ.get('VULKAN_SDK', '')
_KITS = os.path.join(os.environ.get('ProgramFiles(x86)', ''),
                     'Windows Kits', '*', 'bin', '*', 'x64')
DXC = _find('DXC', 'dxc.exe',
            os.path.join(_VK, 'Bin', 'dxc.exe') if _VK else '',
            os.path.join(_KITS, 'dxc.exe'))
FXC = _find('FXC', 'fxc.exe',
            os.path.join(_KITS, 'fxc.exe'))

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
