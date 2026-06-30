// Manifest-driven headless D3D11 runtime for the YAML render-graph harness.
// Immediate-context slot binding maps HLSL t#/u# registers directly (no
// descriptor heap). Realizes compute, graphics, transfer and present passes.
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <d3d11_1.h>
#include <vector>
#include <string>
#include <map>
#include <cstdio>
#include <fstream>
#include "rdoc.h"
#include "json.h"

#define HR(x) do { HRESULT _r = (x); if (FAILED(_r)) { \
    printf("HR 0x%08lx at line %d\n", (unsigned long)_r, __LINE__); exit(1); } } while (0)

static ID3D11Device* dev;
static ID3D11DeviceContext* ctx;
static ID3DUserDefinedAnnotation* annot;

struct Res {
    std::string kind;
    ID3D11Buffer* buf = nullptr;
    ID3D11Texture2D* tex = nullptr;
    ID3D11ShaderResourceView* srv = nullptr;
    ID3D11UnorderedAccessView* uav = nullptr;
    ID3D11RenderTargetView* rtv = nullptr;
    ID3D11DepthStencilView* dsv = nullptr;
    UINT elems = 0;
};
static std::map<std::string, Res> g_res;

static std::vector<char> readFile(const std::string& p) {
    std::ifstream f(p, std::ios::binary | std::ios::ate);
    if (!f) { printf("cannot open %s\n", p.c_str()); exit(1); }
    size_t n = (size_t)f.tellg();
    std::vector<char> b(n);
    f.seekg(0);
    f.read(b.data(), n);
    return b;
}

static void setName(ID3D11DeviceChild* o, const std::string& n) {
    if (o) o->SetPrivateData(WKPDID_D3DDebugObjectName, (UINT)n.size(), n.c_str());
}

static void makeBuffer(Res& r, const std::string& name) {
    D3D11_BUFFER_DESC d{};
    d.ByteWidth = (r.elems ? r.elems : 4) * 4;
    d.BindFlags = D3D11_BIND_SHADER_RESOURCE | D3D11_BIND_UNORDERED_ACCESS;
    d.MiscFlags = D3D11_RESOURCE_MISC_BUFFER_STRUCTURED;
    d.StructureByteStride = 4;
    HR(dev->CreateBuffer(&d, 0, &r.buf));
    setName(r.buf, name);
}

// Constant buffer: D3D11_BIND_CONSTANT_BUFFER is exclusive (no SRV/UAV flags),
// so a cbuffer resource is created separately and bound via CSSetConstantBuffers.
static void makeCBuf(Res& r, const std::string& name) {
    D3D11_BUFFER_DESC d{};
    UINT bytes = (r.elems ? r.elems : 64) * 4;
    d.ByteWidth = (bytes + 15) & ~15u;     // constant buffers: multiple of 16
    d.BindFlags = D3D11_BIND_CONSTANT_BUFFER;
    d.Usage = D3D11_USAGE_DEFAULT;
    HR(dev->CreateBuffer(&d, 0, &r.buf));
    setName(r.buf, name);
}

// IA buffers: a static 3-vertex / 3-index triangle.
static const float kVertsZero[3 * 4] = {0};
static const UINT kTriIndices[3] = {0, 1, 2};

static void makeVBuf(Res& r, const std::string& name) {
    D3D11_BUFFER_DESC d{};
    d.ByteWidth = sizeof(kVertsZero);
    d.BindFlags = D3D11_BIND_VERTEX_BUFFER;
    d.Usage = D3D11_USAGE_DEFAULT;
    D3D11_SUBRESOURCE_DATA sd{};
    sd.pSysMem = kVertsZero;
    HR(dev->CreateBuffer(&d, &sd, &r.buf));
    setName(r.buf, name);
}

static void makeIBuf(Res& r, const std::string& name) {
    D3D11_BUFFER_DESC d{};
    d.ByteWidth = sizeof(kTriIndices);
    d.BindFlags = D3D11_BIND_INDEX_BUFFER;
    d.Usage = D3D11_USAGE_DEFAULT;
    D3D11_SUBRESOURCE_DATA sd{};
    sd.pSysMem = kTriIndices;
    HR(dev->CreateBuffer(&d, &sd, &r.buf));
    setName(r.buf, name);
}

static void makeTex(Res& r, UINT w, UINT h, const std::string& name) {
    D3D11_TEXTURE2D_DESC td{};
    td.Width = w ? w : 64;
    td.Height = h ? h : 64;
    td.MipLevels = 1;
    td.ArraySize = 1;
    td.Format = DXGI_FORMAT_R32G32B32A32_FLOAT;
    td.SampleDesc.Count = 1;
    td.BindFlags = D3D11_BIND_UNORDERED_ACCESS | D3D11_BIND_SHADER_RESOURCE;
    HR(dev->CreateTexture2D(&td, 0, &r.tex));
    setName(r.tex, name);
}

static ID3D11ShaderResourceView* srvOf(Res& r) {
    if (r.srv) return r.srv;
    D3D11_SHADER_RESOURCE_VIEW_DESC d{};
    d.ViewDimension = D3D11_SRV_DIMENSION_BUFFER;
    d.Format = DXGI_FORMAT_UNKNOWN;
    d.Buffer.FirstElement = 0;
    d.Buffer.NumElements = r.elems;
    HR(dev->CreateShaderResourceView(r.buf, &d, &r.srv));
    return r.srv;
}

static ID3D11UnorderedAccessView* uavOf(Res& r) {
    if (r.uav) return r.uav;
    D3D11_UNORDERED_ACCESS_VIEW_DESC d{};
    if (r.kind == "uav_tex") {
        d.ViewDimension = D3D11_UAV_DIMENSION_TEXTURE2D;
        d.Format = DXGI_FORMAT_R32G32B32A32_FLOAT;
        HR(dev->CreateUnorderedAccessView(r.tex, &d, &r.uav));
    } else {
        d.ViewDimension = D3D11_UAV_DIMENSION_BUFFER;
        d.Format = DXGI_FORMAT_UNKNOWN;
        d.Buffer.FirstElement = 0;
        d.Buffer.NumElements = r.elems;
        HR(dev->CreateUnorderedAccessView(r.buf, &d, &r.uav));
    }
    return r.uav;
}

static void makeColorTex(Res& r, UINT w, UINT h, const std::string& name) {
    D3D11_TEXTURE2D_DESC td{};
    td.Width = w ? w : 64; td.Height = h ? h : 64;
    td.MipLevels = 1; td.ArraySize = 1;
    td.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
    td.SampleDesc.Count = 1;
    td.BindFlags = D3D11_BIND_RENDER_TARGET | D3D11_BIND_SHADER_RESOURCE;
    HR(dev->CreateTexture2D(&td, 0, &r.tex));
    setName(r.tex, name);
    HR(dev->CreateRenderTargetView(r.tex, 0, &r.rtv));
    HR(dev->CreateShaderResourceView(r.tex, 0, &r.srv));
}

static void makeDepthTex(Res& r, UINT w, UINT h, const std::string& name) {
    D3D11_TEXTURE2D_DESC td{};
    td.Width = w ? w : 64; td.Height = h ? h : 64;
    td.MipLevels = 1; td.ArraySize = 1;
    td.Format = DXGI_FORMAT_D32_FLOAT;
    td.SampleDesc.Count = 1;
    td.BindFlags = D3D11_BIND_DEPTH_STENCIL;
    HR(dev->CreateTexture2D(&td, 0, &r.tex));
    setName(r.tex, name);
    HR(dev->CreateDepthStencilView(r.tex, 0, &r.dsv));
}

static void makeSampledTex(Res& r, UINT w, UINT h, const std::string& name) {
    D3D11_TEXTURE2D_DESC td{};
    td.Width = w ? w : 64; td.Height = h ? h : 64;
    td.MipLevels = 1; td.ArraySize = 1;
    td.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
    td.SampleDesc.Count = 1;
    td.BindFlags = D3D11_BIND_SHADER_RESOURCE;
    HR(dev->CreateTexture2D(&td, 0, &r.tex));
    setName(r.tex, name);
    HR(dev->CreateShaderResourceView(r.tex, 0, &r.srv));
}

static ID3D11DepthStencilState* depthState(const std::string& acc) {
    D3D11_DEPTH_STENCIL_DESC d{};
    d.DepthEnable = TRUE;
    if (acc == "read") {
        d.DepthWriteMask = D3D11_DEPTH_WRITE_MASK_ZERO;
        d.DepthFunc = D3D11_COMPARISON_LESS_EQUAL;
    } else if (acc == "rw") {
        d.DepthWriteMask = D3D11_DEPTH_WRITE_MASK_ALL;
        d.DepthFunc = D3D11_COMPARISON_LESS_EQUAL;
    } else {  // write: test always passes, depth written
        d.DepthWriteMask = D3D11_DEPTH_WRITE_MASK_ALL;
        d.DepthFunc = D3D11_COMPARISON_ALWAYS;
    }
    ID3D11DepthStencilState* s;
    HR(dev->CreateDepthStencilState(&d, &s));
    return s;
}

static void runGraphicsPass(const js::Value& p, const std::string& shaderDir) {
    auto vsc = readFile(shaderDir + "/" + p["vs"].asStr() + ".cso");
    auto psc = readFile(shaderDir + "/" + p["ps"].asStr() + ".cso");
    ID3D11VertexShader* vs;
    ID3D11PixelShader* ps;
    HR(dev->CreateVertexShader(vsc.data(), vsc.size(), 0, &vs));
    HR(dev->CreatePixelShader(psc.data(), psc.size(), 0, &ps));

    const js::Value& colors = p["color"];
    const js::Value& depthJ = p["depth"];
    const js::Value& sample = p["sample"];

    std::vector<ID3D11RenderTargetView*> rtvs;
    for (size_t i = 0; i < colors.size(); i++)
        rtvs.push_back(g_res[colors[i].asStr()].rtv);
    ID3D11DepthStencilView* dsv = depthJ.isNull() ? nullptr
                                                  : g_res[depthJ["res"].asStr()].dsv;
    ctx->OMSetRenderTargets((UINT)rtvs.size(), rtvs.empty() ? nullptr : rtvs.data(), dsv);
    if (!depthJ.isNull()) {
        ID3D11DepthStencilState* dss = depthState(depthJ["access"].asStr());
        ctx->OMSetDepthStencilState(dss, 0);
    }
    D3D11_VIEWPORT vp{0, 0, 64, 64, 0, 1};
    ctx->RSSetViewports(1, &vp);

    // input-assembler streams: one float4 ATTR per vertex buffer (slot i), and an
    // indexed draw if an index buffer is given. Otherwise a no-IA fullscreen tri.
    const js::Value& vtx = p["vertex"];
    const js::Value& idx = p["index"];
    ID3D11InputLayout* layout = nullptr;
    if (vtx.size() > 0) {
        std::vector<D3D11_INPUT_ELEMENT_DESC> elems;
        for (size_t i = 0; i < vtx.size(); i++) {
            D3D11_INPUT_ELEMENT_DESC e{};
            e.SemanticName = "ATTR";
            e.SemanticIndex = (UINT)i;
            e.Format = DXGI_FORMAT_R32G32B32A32_FLOAT;
            e.InputSlot = (UINT)i;
            e.InputSlotClass = D3D11_INPUT_PER_VERTEX_DATA;
            elems.push_back(e);
        }
        HR(dev->CreateInputLayout(elems.data(), (UINT)elems.size(),
                                  vsc.data(), vsc.size(), &layout));
    }
    ctx->IASetInputLayout(layout);
    ctx->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
    ctx->VSSetShader(vs, 0, 0);
    ctx->PSSetShader(ps, 0, 0);
    std::vector<ID3D11ShaderResourceView*> srvs;
    for (size_t i = 0; i < sample.size(); i++) {
        UINT slot = (UINT)sample[i]["reg_index"].asInt();
        if (srvs.size() <= slot) srvs.resize(slot + 1, nullptr);
        srvs[slot] = g_res[sample[i]["res"].asStr()].srv;
    }
    if (!srvs.empty())
        ctx->PSSetShaderResources(0, (UINT)srvs.size(), srvs.data());

    if (vtx.size() > 0) {
        std::vector<ID3D11Buffer*> vbs;
        std::vector<UINT> strides, offsets;
        for (size_t i = 0; i < vtx.size(); i++) {
            vbs.push_back(g_res[vtx[i].asStr()].buf);
            strides.push_back(16);
            offsets.push_back(0);
        }
        ctx->IASetVertexBuffers(0, (UINT)vbs.size(), vbs.data(),
                                strides.data(), offsets.data());
        if (!idx.isNull()) {
            ctx->IASetIndexBuffer(g_res[idx.asStr()].buf, DXGI_FORMAT_R32_UINT, 0);
            ctx->DrawIndexed(3, 0, 0);
        } else {
            ctx->Draw(3, 0);
        }
    } else {
        ctx->Draw(3, 0);
    }
    if (layout) layout->Release();
}

static std::wstring widen(const std::string& s) {
    return std::wstring(s.begin(), s.end());
}

// nested-marker stack
static std::vector<std::string> g_open;
static void syncMarkers(const std::vector<std::string>& target) {
    size_t common = 0;
    while (common < g_open.size() && common < target.size() &&
           g_open[common] == target[common]) common++;
    while (g_open.size() > common) {
        if (annot) annot->EndEvent();
        g_open.pop_back();
    }
    while (g_open.size() < target.size()) {
        const std::string& name = target[g_open.size()];
        if (annot) annot->BeginEvent(widen(name).c_str());
        g_open.push_back(name);
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
        printf("usage: generic_d3d11 <manifest.json> <shader_dir> <capture_template>\n");
        return 1;
    }
    std::string manifestPath = argv[1], shaderDir = argv[2], capTemplate = argv[3];
    js::Value M = js::parse_file(manifestPath);

    Rdoc rdoc;
    rdoc.init(capTemplate.c_str());

    D3D_FEATURE_LEVEL fl = D3D_FEATURE_LEVEL_11_0;
    HR(D3D11CreateDevice(0, D3D_DRIVER_TYPE_HARDWARE, 0, 0, &fl, 1,
                         D3D11_SDK_VERSION, &dev, 0, &ctx));
    ctx->QueryInterface(__uuidof(ID3DUserDefinedAnnotation), (void**)&annot);

    const js::Value& resources = M["resources"];
    for (size_t i = 0; i < resources.size(); i++) {
        const js::Value& r = resources[i];
        std::string name = r["name"].asStr();
        std::string kind = r["kind"].asStr();
        Res res;
        res.kind = kind;
        UINT w = 64, h = 64;
        if (!r["dims"].isNull() && r["dims"].size() >= 2) {
            w = (UINT)r["dims"][0].asInt();
            h = (UINT)r["dims"][1].asInt();
        }
        if (kind == "buffer") {
            res.elems = r["elements"].isNull() ? 256 : (UINT)r["elements"].asInt();
            makeBuffer(res, name);
        } else if (kind == "cbuffer") {
            res.elems = r["elements"].isNull() ? 64 : (UINT)r["elements"].asInt();
            makeCBuf(res, name);
        } else if (kind == "vbuffer") {
            makeVBuf(res, name);
        } else if (kind == "ibuffer") {
            makeIBuf(res, name);
        } else if (kind == "uav_tex") {
            makeTex(res, w, h, name);
        } else if (kind == "color") {
            makeColorTex(res, w, h, name);
        } else if (kind == "depth") {
            makeDepthTex(res, w, h, name);
        } else if (kind == "sampled") {
            makeSampledTex(res, w, h, name);
        } else {
            continue;  // swapchain: later phases
        }
        g_res[name] = res;
    }

    const js::Value& passes = M["passes"];
    std::vector<ID3D11ComputeShader*> shaders(passes.size(), nullptr);
    for (size_t i = 0; i < passes.size(); i++) {
        const js::Value& p = passes[i];
        if (p["type"].asStr() != "compute") continue;
        auto code = readFile(shaderDir + "/" + p["cs"].asStr() + ".cso");
        HR(dev->CreateComputeShader(code.data(), code.size(), 0, &shaders[i]));
    }

    // frame-prior compute UAV/SRV views
    if (!M["frame_prior"].isNull() && M["frame_prior"].asBool()) {
        for (size_t i = 0; i < passes.size(); i++) {
            const js::Value& binds = passes[i]["binds"];
            for (size_t j = 0; j < binds.size(); j++) {
                Res& res = g_res[binds[j]["res"].asStr()];
                std::string cls = binds[j]["reg_class"].asStr();
                if (cls == "t") srvOf(res);
                else if (cls == "u") uavOf(res);
            }
        }
    }

    rdoc.begin();
    for (size_t i = 0; i < passes.size(); i++) {
        const js::Value& p = passes[i];
        std::string type = p["type"].asStr();
        if (type != "compute" && type != "graphics" && type != "transfer") continue;

        std::vector<std::string> base;
        const js::Value& scope = p["scope"];
        for (size_t s = 0; s < scope.size(); s++) base.push_back(scope[s].asStr());
        bool markerOn = p["marker"].isNull() ? true : p["marker"].asBool();

        // compute bind arrays (shared across instances)
        const js::Value& binds = p["binds"];
        std::vector<ID3D11ShaderResourceView*> srvs;
        std::vector<ID3D11UnorderedAccessView*> uavs;
        std::vector<ID3D11Buffer*> cbs;
        if (type == "compute") {
            for (size_t j = 0; j < binds.size(); j++) {
                const js::Value& b = binds[j];
                Res& res = g_res[b["res"].asStr()];
                std::string cls = b["reg_class"].asStr();
                UINT slot = (UINT)b["reg_index"].asInt();
                if (cls == "t") {
                    if (srvs.size() <= slot) srvs.resize(slot + 1, nullptr);
                    srvs[slot] = srvOf(res);
                } else if (cls == "u") {
                    if (uavs.size() <= slot) uavs.resize(slot + 1, nullptr);
                    uavs[slot] = uavOf(res);
                } else if (cls == "b") {
                    if (cbs.size() <= slot) cbs.resize(slot + 1, nullptr);
                    cbs[slot] = res.buf;
                }
            }
        }

        for (const std::string& inst : instanceNames(p)) {
            std::vector<std::string> target = base;
            if (markerOn) target.push_back(inst);
            syncMarkers(target);

            if (type == "graphics") {
                runGraphicsPass(p, shaderDir);
                continue;
            }
            if (type == "transfer") {
                const js::Value& copy = p["copy"];
                ctx->CopyResource(g_res[copy["dst"].asStr()].buf,
                                  g_res[copy["src"].asStr()].buf);
                continue;
            }
            ctx->CSSetShader(shaders[i], 0, 0);
            if (!srvs.empty())
                ctx->CSSetShaderResources(0, (UINT)srvs.size(), srvs.data());
            if (!uavs.empty())
                ctx->CSSetUnorderedAccessViews(0, (UINT)uavs.size(), uavs.data(), 0);
            if (!cbs.empty())
                ctx->CSSetConstantBuffers(0, (UINT)cbs.size(), cbs.data());
            int groups = p["groups"].isNull() ? 1 : p["groups"].asInt();
            ctx->Dispatch(groups ? groups : 1, 1, 1);
        }
    }
    syncMarkers({});
    ctx->Flush();
    rdoc.end();

    printf("generic_d3d11 done: %s\n", M["name"].asStr().c_str());
    return 0;
}
