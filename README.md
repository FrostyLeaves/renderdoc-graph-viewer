# Graph Viewer — a RenderDoc extension

A dockable [RenderDoc](https://renderdoc.org/) (qrenderdoc) panel that turns a whole capture into an interactive, **frame-graph–style preview**: a quick view of how each pass (graphics or compute) relates to each resource (render target or buffer), with one edge per read or write between them.

![Legend: every node colour, shape and edge the graph uses](docs/images/legend.png)

**English** · [简体中文](README.zh-CN.md)

## Node reference

Passes and resources alternate, with each edge representing a single read or write. The figure above shows the whole visual language in one frame. The table below names each node — **each row's icon is that exact node**.

| Node | Detail |
|---|---|
| <img src="docs/images/node-graphics.png" height="44"> | **Graphics Pass** — a rasterised draw or batch of draws. Writes colour and/or depth attachments and reads the textures and buffers it samples. This is the workhorse of most frames. |
| <img src="docs/images/node-compute.png" height="44"> | **Compute Pass** — a compute dispatch. Reads and writes UAV textures and buffers with no fixed-function raster — culling, lighting, particle sim, post-processing. |
| <img src="docs/images/node-present.png" height="44"> | **Present** — the frame's present / swap. Reads the final swapchain image and sits right-most in submission order. |
| <img src="docs/images/node-scope.png" height="58"> | **Sub-graph** (stacked cards) — an aggregate node, one per RenderDoc debug-marker region (a camera, a render phase...). It simplifies the current graph preview. Double-click to enter the sub-graph. |
| <img src="docs/images/node-portal.png" height="44"> | **Portal** — stands for an external sub-graph that has an input/output relationship with the current one, keeping cross-sub-graph dependencies visible. Double-click to jump. |
| <img src="docs/images/node-color.png" height="44"> | **Render Target** — a texture written as a colour attachment. Click the eye icon for a quick thumbnail preview. |
| <img src="docs/images/node-depth.png" height="44"> | **Depth Target** — the depth / stencil buffer. |
| <img src="docs/images/node-buffer.png" height="44"> | **Buffer** — any GPU buffer (vertex, index, cbuffer, or UAV). |
| <img src="docs/images/node-swapchain.png" height="44"> | **Swapchain** — the frame's final output. |
| <img src="docs/images/node-external.png" height="44"> | **External / scope input** — a node whose content is produced outside the current view, drawn with a hatched background. May be a previous-frame history RT or a CPU-uploaded resource. |

**Edges** are coloured by direction: **green** = read, **red** = write. A **dashed** edge is bound to the pipeline but never sampled by the shader.

## Features

### Write versions

![A sub-graph: reads, writes, write-versions, scope inputs, portals](docs/images/core-concepts.png)

A resource written more than once in the current graph is split into one node per *write episode*, so each read edge points exactly at the write that produced the content it consumes. Selecting one version outlines all its siblings. This makes every edge flow strictly left-to-right with no back-references, which keeps the graph from tangling and makes the write order of a single resource explicit.

### Behaviour bundling

![18 identical copies merged into one ×18 node with a member list](docs/images/bundling.png)

Nodes that do the **same thing** collapse into one. A bundle lists every member, folding past 24. **Double-click a row to jump straight to it**. Above, 18 consecutive `CopyBufferRegion()` calls become a single `×18` node.

Bundling is heuristic. All three conditions must hold:

1. identical edge structure (a resource by its writer-set + reader-set + category, and a pass by its read-set + write-set + category)
2. name similarity (split on separators / camelCase / digit boundaries, matching first+last token structure with digits normalised)
3. ≥3 members, contiguous in submission order, on the same resource versions.

### Sub-graphs & portals

![Top-level sub-graphs: Scene Rendering reads four inputs and writes two outputs — double-click it to drill in](docs/images/feat-scope.png)

Most of the time a whole-frame graph is too large to lay out flat. RenderDoc groups a frame into nested **debug-marker regions** (a camera, a shadow pass, a post chain...), and we turn each marker into a **sub-graph node** — a card-stacked node that stands in for everything submitted inside it, which keeps the graph readable.

Double-click a **sub-graph** node to open it and preview the passes and resources *inside* that marker.

Drill into **Scene Rendering** above and you see its internal pass structure, with the inputs it consumes shown as external nodes inside the sub-graph:

![Scene Rendering drilled in: the same four inputs and two outputs as above, now carried by producer / consumer portals at the boundary](docs/images/feat-scope-in.png)

The related external sub-graphs appear inside as **portals** — e.g. the upstream GBuffer and Shadows, and the downstream Post FX.

Double-click a portal to jump to the external sub-graph it stands for. For example, double-clicking the Post FX consumer portal above lands you in the Post FX sub-graph, where the sub-graph you just left now appears as a producer portal feeding it:

![After jumping through a portal: you land in the target sub-graph, breadcrumb updated](docs/images/feat-portal-jump.png)

Switching display options, bundling, or previews preserves the current pan and zoom. Only back or refresh re-fits the view.

### Depth read/write refinement (per-API)

![A depth prepass writes Scene Depth, while a depth-test-only base pass reads it](docs/images/feat-depth.png)

Each API has an adapter that reads the depth buffer's read/write state in the current graphics pipeline, distinguishing read, write, or read-write from the pipeline's depth-test state.

### Unused-binding detection

![A bound-but-unsampled resource drawn with a dashed read edge](docs/images/feat-unused.png)

Optional descriptor-usage analysis dashes the read edge of a resource that is bound to the pipeline but never referenced by the shader, so dead bindings stand out. Toggle it from the config band. It rarely hits, but when it does, it is worth checking whether you have redundant resource bindings you do not need.

### Config band

![The config band: Show / Features / Resource-candidate sections](docs/images/feat-config.png)

The config band drops down from the toolbar. Every switch batches behind Apply — nothing changes the graph until you click it. State persists to `%APPDATA%\qrenderdoc\renderdoc_graph_viewer.json`.

## Install & usage

Requires an **official RenderDoc build ≥ 1.33** (for its bundled PySide2).

1. Copy the `renderdoc_graph_viewer` folder into `%APPDATA%\qrenderdoc\extensions`, so you end up with `…\qrenderdoc\extensions\renderdoc_graph_viewer\extension.json`.

2. In RenderDoc: **Tools → Manage Extensions → tick "renderdoc-graph-viewer"** (optionally Always Load).

Load a capture, then open **Window → Graph Viewer**. The panel parses the capture automatically when opened.

## Tested graphics APIs

Parsing is backend-agnostic; tested on:

| API | Status |
|---|---|
| Direct3D 12 | ✅ Tested |
| Direct3D 11 | ✅ Tested |
| Vulkan | ✅ Tested |
| OpenGL | ⚠️ Not tested |

## Known limitations

- Dependencies are at **whole-texture / whole-buffer** granularity (no mip/slice).
- A resource bound to a descriptor but never sampled still produces a read edge (matching `GetUsage`), though the optional unused-binding scan dashes the ones it can prove.
- Thumbnails only preview ordinary textures, and some formats may fail.

## License

[MIT](LICENSE)
