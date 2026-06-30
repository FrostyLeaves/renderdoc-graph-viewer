// Manifest-driven headless D3D12 runtime for the YAML render-graph harness.
// Each compute pass gets a root signature matching its binding layout and a
// contiguous shader-visible heap segment. A hidden swapchain is created for replay.
// Graphics, transfer and present passes are realized the same way.
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <d3d12.h>
#include <dxgi1_4.h>
#include <vector>
#include <string>
#include <map>
#include <cstdio>
#include <fstream>
#include "rdoc.h"
#include "json.h"

#define HR(x) do { HRESULT _r = (x); if (FAILED(_r)) { \
    printf("HR 0x%08lx at line %d\n", (unsigned long)_r, __LINE__); exit(1); } } while (0)

static ID3D12Device* dev;
static UINT incr;
static ID3D12DescriptorHeap* heap;
static D3D12_CPU_DESCRIPTOR_HANDLE heapCpu0;
static D3D12_GPU_DESCRIPTOR_HANDLE heapGpu0;

struct Res {
    std::string kind;
    ID3D12Resource* res = nullptr;
    UINT elems = 0;
    UINT rtv = 0xffffffff;   // index in rtvHeap (color)
    UINT dsv = 0xffffffff;   // index in dsvHeap (depth)
    D3D12_RESOURCE_STATES state = D3D12_RESOURCE_STATE_COMMON;
};
static std::map<std::string, Res> g_res;
static ID3D12DescriptorHeap* rtvHeap = nullptr;
static UINT rtvIncr = 0, rtvNext = 0;
static ID3D12DescriptorHeap* dsvHeap = nullptr;
static UINT dsvIncr = 0, dsvNext = 0;

static std::vector<char> readFile(const std::string& p) {
    std::ifstream f(p, std::ios::binary | std::ios::ate);
    if (!f) { printf("cannot open %s\n", p.c_str()); exit(1); }
    size_t n = (size_t)f.tellg();
    std::vector<char> b(n);
    f.seekg(0);
    f.read(b.data(), n);
    return b;
}

static void nameRes(ID3D12Resource* r, const std::string& n) {
    std::wstring w(n.begin(), n.end());
    r->SetName(w.c_str());
}

static ID3D12Resource* makeBuf(UINT bytes, const std::string& name) {
    D3D12_HEAP_PROPERTIES hp{};
    hp.Type = D3D12_HEAP_TYPE_DEFAULT;
    D3D12_RESOURCE_DESC rd{};
    rd.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER;
    rd.Width = bytes < 16 ? 16 : bytes;
    rd.Height = 1;
    rd.DepthOrArraySize = 1;
    rd.MipLevels = 1;
    rd.SampleDesc.Count = 1;
    rd.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
    rd.Flags = D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS;
    ID3D12Resource* r;
    HR(dev->CreateCommittedResource(&hp, D3D12_HEAP_FLAG_NONE, &rd,
        D3D12_RESOURCE_STATE_COMMON, 0, IID_PPV_ARGS(&r)));
    nameRes(r, name);
    return r;
}

// Constant buffer: a plain default-heap buffer (no UAV flag) bound through a root
// CBV. Width is rounded to 256 (D3D12 CBV size/address alignment). Contents are
// uninitialized -- only the CS_Constants read edge matters to the graph.
static ID3D12Resource* makeCBuf(UINT bytes, const std::string& name) {
    D3D12_HEAP_PROPERTIES hp{};
    hp.Type = D3D12_HEAP_TYPE_DEFAULT;
    D3D12_RESOURCE_DESC rd{};
    rd.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER;
    rd.Width = bytes < 256 ? 256 : ((bytes + 255) & ~255u);
    rd.Height = 1;
    rd.DepthOrArraySize = 1;
    rd.MipLevels = 1;
    rd.SampleDesc.Count = 1;
    rd.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
    rd.Flags = D3D12_RESOURCE_FLAG_NONE;
    ID3D12Resource* r;
    HR(dev->CreateCommittedResource(&hp, D3D12_HEAP_FLAG_NONE, &rd,
        D3D12_RESOURCE_STATE_COMMON, 0, IID_PPV_ARGS(&r)));
    nameRes(r, name);
    return r;
}

// IA buffers live on an UPLOAD heap as a static 3-vertex / 3-index triangle.
static const float kVertsZero[3 * 4] = {0};
static const UINT kTriIndices[3] = {0, 1, 2};

static ID3D12Resource* makeUpload(UINT bytes, const void* data,
                                  const std::string& name) {
    D3D12_HEAP_PROPERTIES hp{};
    hp.Type = D3D12_HEAP_TYPE_UPLOAD;
    D3D12_RESOURCE_DESC rd{};
    rd.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER;
    rd.Width = bytes < 16 ? 16 : bytes;
    rd.Height = 1;
    rd.DepthOrArraySize = 1;
    rd.MipLevels = 1;
    rd.SampleDesc.Count = 1;
    rd.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
    ID3D12Resource* r;
    HR(dev->CreateCommittedResource(&hp, D3D12_HEAP_FLAG_NONE, &rd,
        D3D12_RESOURCE_STATE_GENERIC_READ, 0, IID_PPV_ARGS(&r)));
    if (data) {
        void* p;
        D3D12_RANGE none{0, 0};
        HR(r->Map(0, &none, &p));
        memcpy(p, data, bytes);
        r->Unmap(0, 0);
    }
    nameRes(r, name);
    return r;
}

static ID3D12Resource* makeTex(UINT w, UINT h, const std::string& name) {
    D3D12_HEAP_PROPERTIES hp{};
    hp.Type = D3D12_HEAP_TYPE_DEFAULT;
    D3D12_RESOURCE_DESC rd{};
    rd.Dimension = D3D12_RESOURCE_DIMENSION_TEXTURE2D;
    rd.Width = w ? w : 64;
    rd.Height = h ? h : 64;
    rd.DepthOrArraySize = 1;
    rd.MipLevels = 1;
    rd.Format = DXGI_FORMAT_R32G32B32A32_FLOAT;
    rd.SampleDesc.Count = 1;
    rd.Flags = D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS;
    ID3D12Resource* r;
    HR(dev->CreateCommittedResource(&hp, D3D12_HEAP_FLAG_NONE, &rd,
        D3D12_RESOURCE_STATE_UNORDERED_ACCESS, 0, IID_PPV_ARGS(&r)));
    nameRes(r, name);
    return r;
}

static D3D12_CPU_DESCRIPTOR_HANDLE cpuAt(UINT i) {
    D3D12_CPU_DESCRIPTOR_HANDLE h = heapCpu0;
    h.ptr += (SIZE_T)i * incr;
    return h;
}
static D3D12_GPU_DESCRIPTOR_HANDLE gpuAt(UINT i) {
    D3D12_GPU_DESCRIPTOR_HANDLE h = heapGpu0;
    h.ptr += (UINT64)i * incr;
    return h;
}

static void bufSRV(Res& r, UINT slot) {
    D3D12_SHADER_RESOURCE_VIEW_DESC d{};
    d.ViewDimension = D3D12_SRV_DIMENSION_BUFFER;
    d.Format = DXGI_FORMAT_UNKNOWN;
    d.Shader4ComponentMapping = D3D12_DEFAULT_SHADER_4_COMPONENT_MAPPING;
    d.Buffer.NumElements = r.elems;
    d.Buffer.StructureByteStride = 4;
    dev->CreateShaderResourceView(r.res, &d, cpuAt(slot));
}
static void bufUAV(Res& r, UINT slot) {
    D3D12_UNORDERED_ACCESS_VIEW_DESC d{};
    d.ViewDimension = D3D12_UAV_DIMENSION_BUFFER;
    d.Format = DXGI_FORMAT_UNKNOWN;
    d.Buffer.NumElements = r.elems;
    d.Buffer.StructureByteStride = 4;
    dev->CreateUnorderedAccessView(r.res, 0, &d, cpuAt(slot));
}
static void texUAV(Res& r, UINT slot) {
    D3D12_UNORDERED_ACCESS_VIEW_DESC d{};
    d.ViewDimension = D3D12_UAV_DIMENSION_TEXTURE2D;
    d.Format = DXGI_FORMAT_R32G32B32A32_FLOAT;
    dev->CreateUnorderedAccessView(r.res, 0, &d, cpuAt(slot));
}

// Root param 0 (when present) is one descriptor table: SRV range (t0..) then UAV
// range (u0..). Each cbv adds a root CBV parameter after the table (b# registers),
// bound directly via SetComputeRootConstantBufferView -- no heap descriptor.
static ID3D12RootSignature* makeRootSig(UINT nSrv, UINT nUav,
                                        const std::vector<UINT>& cbvRegs) {
    D3D12_DESCRIPTOR_RANGE ranges[2];
    UINT nr = 0;
    if (nSrv) {
        ranges[nr] = {};
        ranges[nr].RangeType = D3D12_DESCRIPTOR_RANGE_TYPE_SRV;
        ranges[nr].NumDescriptors = nSrv;
        ranges[nr].BaseShaderRegister = 0;
        ranges[nr].OffsetInDescriptorsFromTableStart = 0;
        nr++;
    }
    if (nUav) {
        ranges[nr] = {};
        ranges[nr].RangeType = D3D12_DESCRIPTOR_RANGE_TYPE_UAV;
        ranges[nr].NumDescriptors = nUav;
        ranges[nr].BaseShaderRegister = 0;
        ranges[nr].OffsetInDescriptorsFromTableStart = nSrv;
        nr++;
    }
    std::vector<D3D12_ROOT_PARAMETER> params;
    if (nr) {
        D3D12_ROOT_PARAMETER rp{};
        rp.ParameterType = D3D12_ROOT_PARAMETER_TYPE_DESCRIPTOR_TABLE;
        rp.DescriptorTable.NumDescriptorRanges = nr;
        rp.DescriptorTable.pDescriptorRanges = ranges;
        params.push_back(rp);
    }
    for (size_t i = 0; i < cbvRegs.size(); i++) {
        D3D12_ROOT_PARAMETER rp{};
        rp.ParameterType = D3D12_ROOT_PARAMETER_TYPE_CBV;
        rp.Descriptor.ShaderRegister = cbvRegs[i];
        rp.Descriptor.RegisterSpace = 0;
        params.push_back(rp);
    }
    D3D12_ROOT_SIGNATURE_DESC rsd{};
    rsd.NumParameters = (UINT)params.size();
    rsd.pParameters = params.empty() ? nullptr : params.data();
    ID3DBlob* sig;
    ID3DBlob* err;
    HR(D3D12SerializeRootSignature(&rsd, D3D_ROOT_SIGNATURE_VERSION_1, &sig, &err));
    ID3D12RootSignature* rootSig;
    HR(dev->CreateRootSignature(0, sig->GetBufferPointer(), sig->GetBufferSize(),
                                IID_PPV_ARGS(&rootSig)));
    return rootSig;
}

static ID3D12PipelineState* makePSO(ID3D12RootSignature* rs, const std::string& dxil) {
    auto code = readFile(dxil);
    D3D12_COMPUTE_PIPELINE_STATE_DESC d{};
    d.pRootSignature = rs;
    d.CS.pShaderBytecode = code.data();
    d.CS.BytecodeLength = code.size();
    ID3D12PipelineState* pso;
    HR(dev->CreateComputePipelineState(&d, IID_PPV_ARGS(&pso)));
    return pso;
}

// ---- graphics resources / views ----
static ID3D12Resource* makeRT(UINT w, UINT h, const std::string& name) {
    D3D12_HEAP_PROPERTIES hp{}; hp.Type = D3D12_HEAP_TYPE_DEFAULT;
    D3D12_RESOURCE_DESC rd{};
    rd.Dimension = D3D12_RESOURCE_DIMENSION_TEXTURE2D;
    rd.Width = w ? w : 64; rd.Height = h ? h : 64;
    rd.DepthOrArraySize = 1; rd.MipLevels = 1;
    rd.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
    rd.SampleDesc.Count = 1;
    rd.Flags = D3D12_RESOURCE_FLAG_ALLOW_RENDER_TARGET;
    ID3D12Resource* r;
    HR(dev->CreateCommittedResource(&hp, D3D12_HEAP_FLAG_NONE, &rd,
        D3D12_RESOURCE_STATE_COMMON, 0, IID_PPV_ARGS(&r)));
    nameRes(r, name);
    return r;
}

static ID3D12Resource* makeDS(UINT w, UINT h, const std::string& name) {
    D3D12_HEAP_PROPERTIES hp{}; hp.Type = D3D12_HEAP_TYPE_DEFAULT;
    D3D12_RESOURCE_DESC rd{};
    rd.Dimension = D3D12_RESOURCE_DIMENSION_TEXTURE2D;
    rd.Width = w ? w : 64; rd.Height = h ? h : 64;
    rd.DepthOrArraySize = 1; rd.MipLevels = 1;
    rd.Format = DXGI_FORMAT_D32_FLOAT;
    rd.SampleDesc.Count = 1;
    rd.Flags = D3D12_RESOURCE_FLAG_ALLOW_DEPTH_STENCIL;
    D3D12_CLEAR_VALUE cv{};
    cv.Format = DXGI_FORMAT_D32_FLOAT;
    cv.DepthStencil.Depth = 1.0f;
    ID3D12Resource* r;
    HR(dev->CreateCommittedResource(&hp, D3D12_HEAP_FLAG_NONE, &rd,
        D3D12_RESOURCE_STATE_DEPTH_WRITE, &cv, IID_PPV_ARGS(&r)));
    nameRes(r, name);
    return r;
}

static ID3D12Resource* makeSampledTex(UINT w, UINT h, const std::string& name) {
    D3D12_HEAP_PROPERTIES hp{}; hp.Type = D3D12_HEAP_TYPE_DEFAULT;
    D3D12_RESOURCE_DESC rd{};
    rd.Dimension = D3D12_RESOURCE_DIMENSION_TEXTURE2D;
    rd.Width = w ? w : 64; rd.Height = h ? h : 64;
    rd.DepthOrArraySize = 1; rd.MipLevels = 1;
    rd.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
    rd.SampleDesc.Count = 1;
    ID3D12Resource* r;
    HR(dev->CreateCommittedResource(&hp, D3D12_HEAP_FLAG_NONE, &rd,
        D3D12_RESOURCE_STATE_COMMON, 0, IID_PPV_ARGS(&r)));
    nameRes(r, name);
    return r;
}

static void texSRV(Res& r, UINT slot) {
    D3D12_SHADER_RESOURCE_VIEW_DESC d{};
    d.ViewDimension = D3D12_SRV_DIMENSION_TEXTURE2D;
    d.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
    d.Shader4ComponentMapping = D3D12_DEFAULT_SHADER_4_COMPONENT_MAPPING;
    d.Texture2D.MipLevels = 1;
    dev->CreateShaderResourceView(r.res, &d, cpuAt(slot));
}

static void barrier(ID3D12GraphicsCommandList* cl, ID3D12Resource* res,
                    D3D12_RESOURCE_STATES from, D3D12_RESOURCE_STATES to) {
    if (from == to) return;
    D3D12_RESOURCE_BARRIER b{};
    b.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
    b.Transition.pResource = res;
    b.Transition.StateBefore = from;
    b.Transition.StateAfter = to;
    b.Transition.Subresource = D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
    cl->ResourceBarrier(1, &b);
}

// Graphics root signature: one descriptor table = SRV range (t0..) for PS samples.
static ID3D12RootSignature* makeGfxRootSig(UINT nSrv) {
    D3D12_DESCRIPTOR_RANGE range{};
    D3D12_ROOT_PARAMETER rp{};
    UINT np = 0;
    if (nSrv) {
        range.RangeType = D3D12_DESCRIPTOR_RANGE_TYPE_SRV;
        range.NumDescriptors = nSrv;
        range.BaseShaderRegister = 0;
        range.OffsetInDescriptorsFromTableStart = 0;
        rp.ParameterType = D3D12_ROOT_PARAMETER_TYPE_DESCRIPTOR_TABLE;
        rp.ShaderVisibility = D3D12_SHADER_VISIBILITY_PIXEL;
        rp.DescriptorTable.NumDescriptorRanges = 1;
        rp.DescriptorTable.pDescriptorRanges = &range;
        np = 1;
    }
    D3D12_ROOT_SIGNATURE_DESC rsd{};
    rsd.NumParameters = np;
    rsd.pParameters = np ? &rp : nullptr;
    rsd.Flags = D3D12_ROOT_SIGNATURE_FLAG_ALLOW_INPUT_ASSEMBLER_INPUT_LAYOUT;
    ID3DBlob* sig; ID3DBlob* err;
    HR(D3D12SerializeRootSignature(&rsd, D3D_ROOT_SIGNATURE_VERSION_1, &sig, &err));
    ID3D12RootSignature* rs;
    HR(dev->CreateRootSignature(0, sig->GetBufferPointer(), sig->GetBufferSize(),
                                IID_PPV_ARGS(&rs)));
    return rs;
}

static ID3D12PipelineState* makeGfxPSO(ID3D12RootSignature* rs,
        const std::string& vsPath, const std::string& psPath,
        UINT nColor, bool hasDepth, const std::string& depthAcc, UINT nStreams) {
    auto vs = readFile(vsPath);
    auto ps = readFile(psPath);
    D3D12_GRAPHICS_PIPELINE_STATE_DESC d{};
    d.pRootSignature = rs;
    d.VS = {vs.data(), vs.size()};
    d.PS = {ps.data(), ps.size()};
    // one float4 ATTR per vertex stream, each from its own input slot
    std::vector<D3D12_INPUT_ELEMENT_DESC> elems;
    for (UINT i = 0; i < nStreams; i++) {
        D3D12_INPUT_ELEMENT_DESC e{};
        e.SemanticName = "ATTR";
        e.SemanticIndex = i;
        e.Format = DXGI_FORMAT_R32G32B32A32_FLOAT;
        e.InputSlot = i;
        e.InputSlotClass = D3D12_INPUT_CLASSIFICATION_PER_VERTEX_DATA;
        elems.push_back(e);
    }
    d.InputLayout = {elems.empty() ? nullptr : elems.data(), (UINT)elems.size()};
    d.BlendState.RenderTarget[0].RenderTargetWriteMask = 0xF;
    for (UINT i = 1; i < 8; i++) d.BlendState.RenderTarget[i] = d.BlendState.RenderTarget[0];
    d.SampleMask = 0xffffffff;
    d.RasterizerState.FillMode = D3D12_FILL_MODE_SOLID;
    d.RasterizerState.CullMode = D3D12_CULL_MODE_NONE;
    if (hasDepth) {
        d.DepthStencilState.DepthEnable = TRUE;
        if (depthAcc == "read") {
            d.DepthStencilState.DepthWriteMask = D3D12_DEPTH_WRITE_MASK_ZERO;
            d.DepthStencilState.DepthFunc = D3D12_COMPARISON_FUNC_LESS_EQUAL;
        } else if (depthAcc == "rw") {
            d.DepthStencilState.DepthWriteMask = D3D12_DEPTH_WRITE_MASK_ALL;
            d.DepthStencilState.DepthFunc = D3D12_COMPARISON_FUNC_LESS_EQUAL;
        } else {  // write
            d.DepthStencilState.DepthWriteMask = D3D12_DEPTH_WRITE_MASK_ALL;
            d.DepthStencilState.DepthFunc = D3D12_COMPARISON_FUNC_ALWAYS;
        }
        d.DSVFormat = DXGI_FORMAT_D32_FLOAT;
    }
    d.PrimitiveTopologyType = D3D12_PRIMITIVE_TOPOLOGY_TYPE_TRIANGLE;
    d.NumRenderTargets = nColor;
    for (UINT i = 0; i < nColor; i++) d.RTVFormats[i] = DXGI_FORMAT_R8G8B8A8_UNORM;
    d.SampleDesc.Count = 1;
    ID3D12PipelineState* pso;
    HR(dev->CreateGraphicsPipelineState(&d, IID_PPV_ARGS(&pso)));
    return pso;
}

static D3D12_CPU_DESCRIPTOR_HANDLE rtvAt(UINT i) {
    D3D12_CPU_DESCRIPTOR_HANDLE h = rtvHeap->GetCPUDescriptorHandleForHeapStart();
    h.ptr += (SIZE_T)i * rtvIncr;
    return h;
}
static D3D12_CPU_DESCRIPTOR_HANDLE dsvAt(UINT i) {
    D3D12_CPU_DESCRIPTOR_HANDLE h = dsvHeap->GetCPUDescriptorHandleForHeapStart();
    h.ptr += (SIZE_T)i * dsvIncr;
    return h;
}

static std::vector<std::string> g_open;
static void d12Markers(ID3D12GraphicsCommandList* cl,
                       const std::vector<std::string>& target) {
    size_t common = 0;
    while (common < g_open.size() && common < target.size() &&
           g_open[common] == target[common]) common++;
    while (g_open.size() > common) { cl->EndEvent(); g_open.pop_back(); }
    while (g_open.size() < target.size()) {
        std::wstring w(target[g_open.size()].begin(), target[g_open.size()].end());
        cl->BeginEvent(0, w.c_str(), (UINT)((w.size() + 1) * sizeof(wchar_t)));
        g_open.push_back(target[g_open.size()]);
    }
}

static std::vector<std::string> instanceNames(const js::Value& p) {
    int rep = p["repeat"].isNull() ? 1 : p["repeat"].asInt();
    if (rep < 1) rep = 1;
    std::string name = p["name"].asStr();
    std::vector<std::string> out;
    if (rep <= 1) { out.push_back(name); return out; }
    for (int j = 0; j < rep; j++) out.push_back(name + " " + std::to_string(j));
    return out;
}

int main(int argc, char** argv) {
    if (argc < 4) {
        printf("usage: generic_d3d12 <manifest.json> <shader_dir> <capture_template>\n");
        return 1;
    }
    std::string manifestPath = argv[1], shaderDir = argv[2], capTemplate = argv[3];
    js::Value M = js::parse_file(manifestPath);

    Rdoc rdoc;
    rdoc.init(capTemplate.c_str());

    HR(D3D12CreateDevice(0, D3D_FEATURE_LEVEL_11_0, IID_PPV_ARGS(&dev)));
    incr = dev->GetDescriptorHandleIncrementSize(D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV);

    // hidden window + swapchain for D3D12 replay
    WNDCLASSA wc{};
    wc.lpfnWndProc = DefWindowProcA;
    wc.hInstance = GetModuleHandle(0);
    wc.lpszClassName = "d3d12demo";
    RegisterClassA(&wc);
    HWND hwnd = CreateWindowExA(0, "d3d12demo", "demo", WS_OVERLAPPEDWINDOW,
                                0, 0, 64, 64, 0, 0, wc.hInstance, 0);
    D3D12_COMMAND_QUEUE_DESC qd{};
    qd.Type = D3D12_COMMAND_LIST_TYPE_DIRECT;
    ID3D12CommandQueue* queue;
    HR(dev->CreateCommandQueue(&qd, IID_PPV_ARGS(&queue)));
    IDXGIFactory4* factory;
    HR(CreateDXGIFactory2(0, IID_PPV_ARGS(&factory)));
    DXGI_SWAP_CHAIN_DESC1 scd{};
    scd.Width = 64;
    scd.Height = 64;
    scd.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
    scd.SampleDesc.Count = 1;
    scd.BufferUsage = DXGI_USAGE_RENDER_TARGET_OUTPUT;
    scd.BufferCount = 2;
    scd.SwapEffect = DXGI_SWAP_EFFECT_FLIP_DISCARD;
    IDXGISwapChain1* sc;
    HR(factory->CreateSwapChainForHwnd(queue, hwnd, &scd, 0, 0, &sc));
    ID3D12CommandAllocator* alloc;
    HR(dev->CreateCommandAllocator(D3D12_COMMAND_LIST_TYPE_DIRECT, IID_PPV_ARGS(&alloc)));
    ID3D12GraphicsCommandList* cl;
    HR(dev->CreateCommandList(0, D3D12_COMMAND_LIST_TYPE_DIRECT, alloc, 0, IID_PPV_ARGS(&cl)));

    // RTV/DSV heaps sized by color/depth resource counts
    const js::Value& resources = M["resources"];
    UINT nColorRes = 0, nDepthRes = 0;
    for (size_t i = 0; i < resources.size(); i++) {
        std::string k = resources[i]["kind"].asStr();
        if (k == "color" || k == "swapchain") nColorRes++;
        else if (k == "depth") nDepthRes++;
    }
    rtvIncr = dev->GetDescriptorHandleIncrementSize(D3D12_DESCRIPTOR_HEAP_TYPE_RTV);
    dsvIncr = dev->GetDescriptorHandleIncrementSize(D3D12_DESCRIPTOR_HEAP_TYPE_DSV);
    D3D12_DESCRIPTOR_HEAP_DESC rhd{};
    rhd.Type = D3D12_DESCRIPTOR_HEAP_TYPE_RTV;
    rhd.NumDescriptors = nColorRes ? nColorRes : 1;
    HR(dev->CreateDescriptorHeap(&rhd, IID_PPV_ARGS(&rtvHeap)));
    D3D12_DESCRIPTOR_HEAP_DESC dhd{};
    dhd.Type = D3D12_DESCRIPTOR_HEAP_TYPE_DSV;
    dhd.NumDescriptors = nDepthRes ? nDepthRes : 1;
    HR(dev->CreateDescriptorHeap(&dhd, IID_PPV_ARGS(&dsvHeap)));

    // resources
    for (size_t i = 0; i < resources.size(); i++) {
        const js::Value& r = resources[i];
        std::string name = r["name"].asStr(), kind = r["kind"].asStr();
        Res res;
        res.kind = kind;
        UINT w = 64, h = 64;
        if (!r["dims"].isNull() && r["dims"].size() >= 2) {
            w = (UINT)r["dims"][0].asInt();
            h = (UINT)r["dims"][1].asInt();
        }
        if (kind == "buffer") {
            res.elems = r["elements"].isNull() ? 256 : (UINT)r["elements"].asInt();
            res.res = makeBuf(res.elems * 4, name);
            res.state = D3D12_RESOURCE_STATE_COMMON;
        } else if (kind == "cbuffer") {
            res.elems = r["elements"].isNull() ? 64 : (UINT)r["elements"].asInt();
            res.res = makeCBuf(res.elems * 4, name);
            res.state = D3D12_RESOURCE_STATE_COMMON;
        } else if (kind == "vbuffer") {
            res.res = makeUpload(sizeof(kVertsZero), kVertsZero, name);
            res.state = D3D12_RESOURCE_STATE_GENERIC_READ;
        } else if (kind == "ibuffer") {
            res.res = makeUpload(sizeof(kTriIndices), kTriIndices, name);
            res.state = D3D12_RESOURCE_STATE_GENERIC_READ;
        } else if (kind == "uav_tex") {
            res.res = makeTex(w, h, name);
            res.state = D3D12_RESOURCE_STATE_UNORDERED_ACCESS;
        } else if (kind == "color") {
            res.res = makeRT(w, h, name);
            res.rtv = rtvNext++;
            dev->CreateRenderTargetView(res.res, 0, rtvAt(res.rtv));
            res.state = D3D12_RESOURCE_STATE_COMMON;
        } else if (kind == "swapchain") {
            UINT backbuffer = 0;
            IDXGISwapChain3* sc3 = nullptr;
            if (SUCCEEDED(sc->QueryInterface(IID_PPV_ARGS(&sc3)))) {
                backbuffer = sc3->GetCurrentBackBufferIndex();
                sc3->Release();
            }
            HR(sc->GetBuffer(backbuffer, IID_PPV_ARGS(&res.res)));
            nameRes(res.res, name);
            res.rtv = rtvNext++;
            dev->CreateRenderTargetView(res.res, 0, rtvAt(res.rtv));
            res.state = D3D12_RESOURCE_STATE_PRESENT;
        } else if (kind == "depth") {
            res.res = makeDS(w, h, name);
            res.dsv = dsvNext++;
            dev->CreateDepthStencilView(res.res, 0, dsvAt(res.dsv));
            res.state = D3D12_RESOURCE_STATE_DEPTH_WRITE;
        } else if (kind == "sampled") {
            res.res = makeSampledTex(w, h, name);
            res.state = D3D12_RESOURCE_STATE_COMMON;
        } else {
            continue;
        }
        g_res[name] = res;
    }

    // per-pass heap segments (compute: t/u binds; graphics: sampled SRVs)
    const js::Value& passes = M["passes"];
    std::vector<UINT> segBase(passes.size(), 0);
    std::vector<UINT> segSrv(passes.size(), 0), segUav(passes.size(), 0);
    UINT heapSize = 0;
    for (size_t i = 0; i < passes.size(); i++) {
        const js::Value& p = passes[i];
        std::string type = p["type"].asStr();
        UINT nSrv = 0, nUav = 0;
        if (type == "compute") {
            const js::Value& binds = p["binds"];
            for (size_t j = 0; j < binds.size(); j++) {
                std::string cls = binds[j]["reg_class"].asStr();
                if (cls == "t") nSrv++;
                else if (cls == "u") nUav++;
            }
        } else if (type == "graphics") {
            nSrv = (UINT)p["sample"].size();
        } else {
            continue;
        }
        segSrv[i] = nSrv;
        segUav[i] = nUav;
        segBase[i] = heapSize;
        heapSize += nSrv + nUav;
    }
    if (heapSize == 0) heapSize = 1;

    D3D12_DESCRIPTOR_HEAP_DESC hd{};
    hd.Type = D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV;
    hd.NumDescriptors = heapSize;
    hd.Flags = D3D12_DESCRIPTOR_HEAP_FLAG_SHADER_VISIBLE;
    HR(dev->CreateDescriptorHeap(&hd, IID_PPV_ARGS(&heap)));
    heapCpu0 = heap->GetCPUDescriptorHandleForHeapStart();
    heapGpu0 = heap->GetGPUDescriptorHandleForHeapStart();

    // per-pass root sig + PSO -- built INSIDE the capture frame so RenderDoc
    // records the CreateGraphicsPipelineState chunks the depth refiner reads
    // (pre-frame creation is not in the structured file).
    std::vector<ID3D12RootSignature*> rootSigs(passes.size(), nullptr);
    std::vector<ID3D12PipelineState*> psos(passes.size(), nullptr);
    bool framePrior = !M["frame_prior"].isNull() && M["frame_prior"].asBool();
    auto makeDescriptors = [&]() {
        for (size_t i = 0; i < passes.size(); i++) {
            const js::Value& p = passes[i];
            std::string type = p["type"].asStr();
            if (type == "compute") {
                const js::Value& binds = p["binds"];
                for (size_t j = 0; j < binds.size(); j++) {
                    const js::Value& b = binds[j];
                    Res& res = g_res[b["res"].asStr()];
                    std::string cls = b["reg_class"].asStr();
                    UINT ri = (UINT)b["reg_index"].asInt();
                    if (cls == "t") bufSRV(res, segBase[i] + ri);
                    else if (cls == "u") {
                        UINT slot = segBase[i] + segSrv[i] + ri;
                        if (res.kind == "uav_tex") texUAV(res, slot);
                        else bufUAV(res, slot);
                    }
                }
            } else if (type == "graphics") {
                const js::Value& sample = p["sample"];
                for (size_t j = 0; j < sample.size(); j++) {
                    Res& res = g_res[sample[j]["res"].asStr()];
                    texSRV(res, segBase[i] + (UINT)sample[j]["reg_index"].asInt());
                }
            }
        }
    };
    // frame-prior descriptor setup
    if (framePrior) makeDescriptors();

    rdoc.begin();
    for (size_t i = 0; i < passes.size(); i++) {
        const js::Value& p = passes[i];
        std::string type = p["type"].asStr();
        if (type == "compute") {
            std::vector<UINT> cbvRegs;
            const js::Value& binds = p["binds"];
            for (size_t j = 0; j < binds.size(); j++)
                if (binds[j]["reg_class"].asStr() == "b")
                    cbvRegs.push_back((UINT)binds[j]["reg_index"].asInt());
            rootSigs[i] = makeRootSig(segSrv[i], segUav[i], cbvRegs);
            psos[i] = makePSO(rootSigs[i], shaderDir + "/" + p["cs"].asStr() + ".dxil");
        } else if (type == "graphics") {
            rootSigs[i] = makeGfxRootSig(segSrv[i]);
            psos[i] = makeGfxPSO(rootSigs[i],
                                 shaderDir + "/" + p["vs"].asStr() + ".dxil",
                                 shaderDir + "/" + p["ps"].asStr() + ".dxil",
                                 (UINT)p["color"].size(), !p["depth"].isNull(),
                                 p["depth"].isNull() ? "" : p["depth"]["access"].asStr(),
                                 (UINT)p["vertex"].size());
        }
    }

    if (!framePrior) makeDescriptors();   // in-frame (default)

    cl->SetDescriptorHeaps(1, &heap);
    for (size_t i = 0; i < passes.size(); i++) {
        const js::Value& p = passes[i];
        std::string type = p["type"].asStr();
        if (type != "compute" && type != "graphics" && type != "transfer") continue;
        std::vector<std::string> base;
        const js::Value& scope = p["scope"];
        for (size_t s = 0; s < scope.size(); s++) base.push_back(scope[s].asStr());
        bool markerOn = p["marker"].isNull() ? true : p["marker"].asBool();

        for (const std::string& inst : instanceNames(p)) {
            std::vector<std::string> target = base;
            if (markerOn) target.push_back(inst);
            d12Markers(cl, target);

            if (type == "transfer") {
                const js::Value& copy = p["copy"];
                Res& src = g_res[copy["src"].asStr()];
                Res& dst = g_res[copy["dst"].asStr()];
                barrier(cl, src.res, src.state, D3D12_RESOURCE_STATE_COPY_SOURCE);
                src.state = D3D12_RESOURCE_STATE_COPY_SOURCE;
                barrier(cl, dst.res, dst.state, D3D12_RESOURCE_STATE_COPY_DEST);
                dst.state = D3D12_RESOURCE_STATE_COPY_DEST;
                cl->CopyBufferRegion(dst.res, 0, src.res, 0, 16);
                continue;
            }

            if (type == "graphics") {
                const js::Value& colors = p["color"];
                const js::Value& depthJ = p["depth"];
                const js::Value& sample = p["sample"];
                std::vector<D3D12_CPU_DESCRIPTOR_HANDLE> rtvs;
                for (size_t j = 0; j < colors.size(); j++) {
                    Res& cr = g_res[colors[j].asStr()];
                    barrier(cl, cr.res, cr.state, D3D12_RESOURCE_STATE_RENDER_TARGET);
                    cr.state = D3D12_RESOURCE_STATE_RENDER_TARGET;
                    rtvs.push_back(rtvAt(cr.rtv));
                }
                for (size_t j = 0; j < sample.size(); j++) {
                    Res& sr = g_res[sample[j]["res"].asStr()];
                    barrier(cl, sr.res, sr.state, D3D12_RESOURCE_STATE_PIXEL_SHADER_RESOURCE);
                    sr.state = D3D12_RESOURCE_STATE_PIXEL_SHADER_RESOURCE;
                }
                D3D12_CPU_DESCRIPTOR_HANDLE dsv{};
                bool hasDepth = !depthJ.isNull();
                if (hasDepth) dsv = dsvAt(g_res[depthJ["res"].asStr()].dsv);
                cl->OMSetRenderTargets((UINT)rtvs.size(),
                                       rtvs.empty() ? nullptr : rtvs.data(),
                                       FALSE, hasDepth ? &dsv : nullptr);
                D3D12_VIEWPORT vp{0, 0, 64, 64, 0, 1};
                D3D12_RECT scis{0, 0, 64, 64};
                cl->RSSetViewports(1, &vp);
                cl->RSSetScissorRects(1, &scis);
                cl->IASetPrimitiveTopology(D3D_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
                cl->SetGraphicsRootSignature(rootSigs[i]);
                cl->SetPipelineState(psos[i]);
                if (segSrv[i] > 0)
                    cl->SetGraphicsRootDescriptorTable(0, gpuAt(segBase[i]));
                // input-assembler streams: one VBV per vertex buffer, indexed draw
                // when an index buffer is present
                const js::Value& vtx = p["vertex"];
                const js::Value& idxJ = p["index"];
                if (vtx.size() > 0) {
                    std::vector<D3D12_VERTEX_BUFFER_VIEW> vbvs;
                    for (size_t j = 0; j < vtx.size(); j++) {
                        D3D12_VERTEX_BUFFER_VIEW v{};
                        v.BufferLocation =
                            g_res[vtx[j].asStr()].res->GetGPUVirtualAddress();
                        v.SizeInBytes = sizeof(kVertsZero);
                        v.StrideInBytes = 16;
                        vbvs.push_back(v);
                    }
                    cl->IASetVertexBuffers(0, (UINT)vbvs.size(), vbvs.data());
                }
                if (!idxJ.isNull()) {
                    D3D12_INDEX_BUFFER_VIEW ibv{};
                    ibv.BufferLocation =
                        g_res[idxJ.asStr()].res->GetGPUVirtualAddress();
                    ibv.SizeInBytes = sizeof(kTriIndices);
                    ibv.Format = DXGI_FORMAT_R32_UINT;
                    cl->IASetIndexBuffer(&ibv);
                    cl->DrawIndexedInstanced(3, 1, 0, 0, 0);
                } else {
                    cl->DrawInstanced(3, 1, 0, 0);
                }
                continue;
            }

            cl->SetComputeRootSignature(rootSigs[i]);
            cl->SetPipelineState(psos[i]);
            // root param 0 is the descriptor table (when the pass has any SRV/UAV);
            // root CBVs follow at the next parameter slots, in bind order.
            UINT rootParam = 0;
            if (segSrv[i] + segUav[i] > 0)
                cl->SetComputeRootDescriptorTable(rootParam++, gpuAt(segBase[i]));
            const js::Value& dbinds = p["binds"];
            for (size_t j = 0; j < dbinds.size(); j++) {
                if (dbinds[j]["reg_class"].asStr() != "b") continue;
                Res& cbr = g_res[dbinds[j]["res"].asStr()];
                cl->SetComputeRootConstantBufferView(
                    rootParam++, cbr.res->GetGPUVirtualAddress());
            }
            int groups = p["groups"].isNull() ? 1 : p["groups"].asInt();
            cl->Dispatch(groups ? groups : 1, 1, 1);
            D3D12_RESOURCE_BARRIER bar{};
            bar.Type = D3D12_RESOURCE_BARRIER_TYPE_UAV;
            bar.UAV.pResource = 0;
            cl->ResourceBarrier(1, &bar);
        }
    }
    d12Markers(cl, {});

    HR(cl->Close());
    ID3D12CommandList* lists[] = {cl};
    queue->ExecuteCommandLists(1, lists);
    // No explicit Present; capture boundary comes from the in-app API.

    ID3D12Fence* fence;
    HR(dev->CreateFence(0, D3D12_FENCE_FLAG_NONE, IID_PPV_ARGS(&fence)));
    HANDLE ev = CreateEvent(0, FALSE, FALSE, 0);
    HR(queue->Signal(fence, 1));
    fence->SetEventOnCompletion(1, ev);
    WaitForSingleObject(ev, INFINITE);
    rdoc.end();

    printf("generic_d3d12 done: %s\n", M["name"].asStr().c_str());
    return 0;
}
