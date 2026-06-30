# GPU demo test harness (YAML-driven)

A single YAML file describes a render graph; three generic headless renderers
(**Vulkan, D3D11, D3D12**) realize it as real GPU work, RenderDoc captures the
frame, the viewer parses it, and a comparator asserts the parsed graph is
**isomorphic** to the graph the YAML describes. Different YAMLs exercise
different viewer features, so the harness is the regular end-to-end test that the
graph viewer is correct across all three shader bytecodes (SPIR-V, DXBC, DXIL)
and all node/edge kinds — not just on hand-picked captures.

## How it works

```
scene.yaml ─┬─ HLSL codegen ─→ dxc/fxc ─→ *.spv/*.dxil/*.cso ─┐
            ├─ manifest.json ───────────────────────────────→ generic_{vk,d3d11,d3d12}.exe
            │                                                   → RenderDoc capture
            └─ expected.json (oracle) ─────────→ compare ◀──── viewer build_scoped
```

The capture path (real GPU → RenderDoc → viewer) and the oracle path (YAML
intent → expected graph) are independent; a bug on either side shows up as a
comparator diff. The oracle derives its expected graph by feeding a *synthetic*
bundle (the accesses the YAML declares) through the viewer's own `build_scoped`,
then canonicalizing with the same `compare.canon` the verifier uses — so what is
tested end-to-end is the API-specific reconstruction (`shader_refinement`,
`shader_access`, `extract_bundle`, depth refinement) and the scope/portal/bundle
assembly.

## Running

Needs Visual Studio (`cl`), the Vulkan SDK (`dxc`), the Windows SDK (`fxc`), a
GPU, RenderDoc, and `pip install pyyaml` for the host Python.

```
tests\e2e\build_runtime.bat                 # build the 3 generic exes once
python tests\e2e\run_all.py                 # all scenes x all APIs
python tests\e2e\run_all.py compute_chain   # a subset
type tools\_probe_out\verify_all.txt             # per scene/API/mode/level + OVERALL
```

`run_all.py` generates each scene's HLSL/manifest/oracle, compiles the shaders,
runs the three exes to capture, then launches qrenderdoc to verify. The offline
host side (schema/codegen/manifest/oracle/comparator) is tested without a GPU by
`pytest tests/` (see `tests/test_harness.py`).

## Scene YAML

```yaml
name: example
bundling: false              # build the graph with equivalent-node bundling
resources:
  Work:  { kind: buffer,  elements: 256 }
  Img:   { kind: uav_tex, size: [64,64], format: rgba32f }
  Albedo:{ kind: color,   size: [64,64], format: rgba8 }
  Depth: { kind: depth,   size: [64,64], format: d32 }
  Tex:   { kind: sampled, size: [64,64], format: rgba8 }
passes:
  - name: Gen
    type: compute            # compute | graphics | transfer | present
    groups: 4
    bind:                    # res + bind(binding kind) + access(actual shader access)
      - { res: Work, bind: uav_buf, access: write }
  - name: Shade
    scope: [Frame]           # marker path -> sub-graph; omitted = root
    type: graphics
    color: [Albedo]
    depth: { res: Depth, access: rw }   # read | write | rw via depth-stencil state
    sample: [ { res: Tex, bind: sampled, access: read } ]
  - name: Blur
    type: compute
    repeat: 4                # N equivalent instances (bundling input)
    bind: [ { res: Work, bind: uav_buf, access: write } ]
  - name: Copy
    type: transfer
    copy: { src: Work, dst: Img }
```

### bind × access matrix (the read/write refinement contract)

`bind` is what is bound; `access` is what the shader bytecode actually does.

| `bind` | `access` | graph edge |
|--------|----------|------------|
| uav_buf / uav_tex | read | read |
| uav_buf / uav_tex | write | write |
| uav_buf / uav_tex | rw | read + write |
| uav_buf / uav_tex | none | unused (dashed read) |
| srv_buf / sampled | read | read |
| color | - | write |
| depth | read/write/rw | per depth state |

In Vulkan a StructuredBuffer SRV is a storage buffer, so RenderDoc can't tell it
from a UAV until the shader is parsed — the oracle models this per API, which is
why `expected.json` is API-specific.

## What each capture is checked against

For every scene × API, the verifier checks the parsed graph against the oracle in
**both refine modes at every scope level** (recursively walking each drillable
sub-graph, keyed by marker *instance* `name#ordinal` so a marker that occurs more
than once is checked per-occurrence), plus the **merged whole-frame view**
(`build_from_bundle`: full-path
pass grouping + non-versioned one-node-per-resource graph). The live viewer's
interactive path is `build_scoped` (focus mode); the merged builder is the
script/whole-frame path, otherwise only offline-tested.

## Scenes

| scene | covers |
|-------|--------|
| compute_chain | every UAV access direction + storage image + SRV (parity with the old hand-written scene) |
| cbv | a constant/uniform buffer feeding a compute pass → CS_Constants read edge (VK uniform buffer, D3D11 `CSSetConstantBuffers`, D3D12 root CBV) |
| unused | a bound-but-unused UAV → dashed read edge under refinement |
| versioning | write-after-read version splitting |
| scope_portal | nested-marker sub-graph drill-down + producer/consumer portals |
| nested | Frame > {Geometry, Lighting} (2 passes each) → 3-level drill-down + sub-scope portals |
| dual_role | a marker ("Side") that occurs twice — once before Frame (producer), once after (consumer) → Frame's focus view splits it into a producer + consumer portal; both Side instances are drillable, so the verifier keys scope levels by marker instance |
| bundling | equivalent pass + resource collapse |
| generations | a resource bundle driven through ≥4 write generations → generation collapse |
| graphics_depth | color/depth/sampled kinds + depth read/write/rw refinement |
| mrt | two color attachments on one draw |
| vertex_index | indexed draw reading 3 vertex streams + an index buffer → VertexBuffer/IndexBuffer read edges; the 3 equivalent streams bundle into one node, the index buffer stays separate |
| atomic | rw UAV via InterlockedAdd (parser Interlocked recognition) |
| markerless | no debug label → build_passes fine-grouping (Compute #N) |
| transfer | buffer copy → CopySrc/CopyDst |
| frame_prior | descriptors/views created BEFORE the frame (persistent-heap / UE5 pattern) |

Every capture also carries RenderDoc's synthetic End-of-Capture present node, so
the present pass category is covered too.

`frame_prior` is the regression guard for the persistent-descriptor reconstruction:
its runtimes create descriptors before the capture, yet the refined graph must
still match. On D3D12 the in-frame `CreateUAV/SRV` chunks are then empty (0), so
resolution depends entirely on seeding `desc_res` from the heap's Initial Contents
snapshot. Vulkan already reads descriptor-set Initial Contents, and D3D11 records
its view-creation chunks regardless of frame timing, so neither needs a fix
(confirmed: `tools/probe_frameprior_check.py` shows D3D12 resolves with 0 in-frame
view chunks; Vulkan cross-checked on the real GPU-driven test2.rdc).

### Known coverage gaps (offline-tested only)

- **`RES_SWAPCHAIN`** — a real headless swapchain across all three APIs is
  impractical; the present node itself is covered.
- **indirect / MSAA-resolve** — heavier resource setup, not yet added.

## Cross-checked against a real capture

`tools/verify_static_vs_pe.py` cross-checks every static read/write verdict on the
large real-world `test2.rdc` (VXGI, GPU-driven, template descriptor updates)
against per-event ground truth: 551 / 551 match.
