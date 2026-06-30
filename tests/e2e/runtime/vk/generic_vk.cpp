// Manifest-driven headless Vulkan runtime for the YAML render-graph harness.
// Reads manifest.json, creates the declared resources, builds one compute
// pipeline per pass from its binding layout (including bound-but-unused
// bindings, so RenderDoc sees them), emits nested debug-utils markers per the
// pass scope path, dispatches, and captures the frame via the in-app API.
// Graphics, transfer and present passes are realized too.
#include <vulkan/vulkan.h>
#include <vector>
#include <string>
#include <map>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include "rdoc.h"
#include "json.h"

#define VK(x) do { VkResult _r = (x); if (_r) { \
    printf("VK error %d at line %d\n", _r, __LINE__); exit(1); } } while (0)

static VkDevice dev;
static VkPhysicalDevice pd;
static uint32_t qf;
static VkQueue queue;
static VkDescriptorPool dpool;
static VkCommandBuffer cb;
static PFN_vkCmdBeginDebugUtilsLabelEXT beginLabel;
static PFN_vkCmdEndDebugUtilsLabelEXT endLabel;
static PFN_vkSetDebugUtilsObjectNameEXT setName;

static void nameObj(uint64_t handle, VkObjectType type, const char* name) {
    if (!setName) return;
    VkDebugUtilsObjectNameInfoEXT ni{VK_STRUCTURE_TYPE_DEBUG_UTILS_OBJECT_NAME_INFO_EXT};
    ni.objectType = type;
    ni.objectHandle = handle;
    ni.pObjectName = name;
    setName(dev, &ni);
}

static uint32_t memType(uint32_t bits, VkMemoryPropertyFlags want) {
    VkPhysicalDeviceMemoryProperties mp;
    vkGetPhysicalDeviceMemoryProperties(pd, &mp);
    for (uint32_t i = 0; i < mp.memoryTypeCount; i++)
        if ((bits & (1u << i)) &&
            ((mp.memoryTypes[i].propertyFlags & want) == want))
            return i;
    return 0;
}

struct Res {
    std::string kind;
    VkBuffer buf = 0;
    VkImage img = 0;
    VkImageView view = 0;
    VkDeviceMemory mem = 0;
    VkFormat format = VK_FORMAT_UNDEFINED;
    VkImageAspectFlags aspect = VK_IMAGE_ASPECT_COLOR_BIT;
};
static std::map<std::string, Res> g_res;
static VkSampler g_sampler = 0;

static Res makeBufUsage(VkDeviceSize sz, VkBufferUsageFlags usage,
                        const char* kind, const char* name) {
    Res r;
    r.kind = kind;
    VkBufferCreateInfo ci{VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO};
    ci.size = sz < 16 ? 16 : sz;
    ci.usage = usage;
    VK(vkCreateBuffer(dev, &ci, 0, &r.buf));
    VkMemoryRequirements mr;
    vkGetBufferMemoryRequirements(dev, r.buf, &mr);
    VkMemoryAllocateInfo ai{VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO};
    ai.allocationSize = mr.size;
    ai.memoryTypeIndex = memType(mr.memoryTypeBits, VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT);
    VK(vkAllocateMemory(dev, &ai, 0, &r.mem));
    vkBindBufferMemory(dev, r.buf, r.mem, 0);
    nameObj((uint64_t)r.buf, VK_OBJECT_TYPE_BUFFER, name);
    return r;
}

static Res makeBuf(VkDeviceSize sz, const char* name) {
    return makeBufUsage(sz, VK_BUFFER_USAGE_STORAGE_BUFFER_BIT |
                        VK_BUFFER_USAGE_TRANSFER_DST_BIT |
                        VK_BUFFER_USAGE_TRANSFER_SRC_BIT, "buffer", name);
}

// Uniform (constant) buffer: VK_BUFFER_USAGE_UNIFORM_BUFFER_BIT makes RenderDoc
// report BufferCategory.Constants and the descriptor a CS_Constants read.
static Res makeCBuf(VkDeviceSize sz, const char* name) {
    return makeBufUsage(sz, VK_BUFFER_USAGE_UNIFORM_BUFFER_BIT |
                        VK_BUFFER_USAGE_TRANSFER_DST_BIT, "cbuffer", name);
}

// IA buffers: host-visible static 3-vertex / 3-index triangle.
static const float kVertsZero[3 * 4] = {0};
static const uint32_t kTriIndices[3] = {0, 1, 2};

static Res makeIABuf(VkDeviceSize sz, VkBufferUsageFlags usage, const void* data,
                     const char* kind, const char* name) {
    Res r;
    r.kind = kind;
    VkBufferCreateInfo ci{VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO};
    ci.size = sz < 16 ? 16 : sz;
    ci.usage = usage;
    VK(vkCreateBuffer(dev, &ci, 0, &r.buf));
    VkMemoryRequirements mr;
    vkGetBufferMemoryRequirements(dev, r.buf, &mr);
    VkMemoryAllocateInfo ai{VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO};
    ai.allocationSize = mr.size;
    ai.memoryTypeIndex = memType(mr.memoryTypeBits,
        VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT);
    VK(vkAllocateMemory(dev, &ai, 0, &r.mem));
    vkBindBufferMemory(dev, r.buf, r.mem, 0);
    if (data) {
        void* p;
        VK(vkMapMemory(dev, r.mem, 0, ci.size, 0, &p));
        memcpy(p, data, (size_t)sz);
        vkUnmapMemory(dev, r.mem);
    }
    nameObj((uint64_t)r.buf, VK_OBJECT_TYPE_BUFFER, name);
    return r;
}

static Res makeImg(uint32_t w, uint32_t h, const char* name) {
    Res r;
    r.kind = "uav_tex";
    VkImageCreateInfo ci{VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO};
    ci.imageType = VK_IMAGE_TYPE_2D;
    ci.format = VK_FORMAT_R32G32B32A32_SFLOAT;
    ci.extent = {w ? w : 64, h ? h : 64, 1};
    ci.mipLevels = 1;
    ci.arrayLayers = 1;
    ci.samples = VK_SAMPLE_COUNT_1_BIT;
    ci.usage = VK_IMAGE_USAGE_STORAGE_BIT | VK_IMAGE_USAGE_SAMPLED_BIT;
    ci.initialLayout = VK_IMAGE_LAYOUT_UNDEFINED;
    VK(vkCreateImage(dev, &ci, 0, &r.img));
    VkMemoryRequirements mr;
    vkGetImageMemoryRequirements(dev, r.img, &mr);
    VkMemoryAllocateInfo ai{VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO};
    ai.allocationSize = mr.size;
    ai.memoryTypeIndex = memType(mr.memoryTypeBits, VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT);
    VK(vkAllocateMemory(dev, &ai, 0, &r.mem));
    vkBindImageMemory(dev, r.img, r.mem, 0);
    VkImageViewCreateInfo vi{VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO};
    vi.image = r.img;
    vi.viewType = VK_IMAGE_VIEW_TYPE_2D;
    vi.format = ci.format;
    vi.subresourceRange = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1};
    VK(vkCreateImageView(dev, &vi, 0, &r.view));
    nameObj((uint64_t)r.img, VK_OBJECT_TYPE_IMAGE, name);
    return r;
}

// Color/depth/sampled images for graphics passes.
static Res makeImage2(uint32_t w, uint32_t h, VkFormat format, VkImageUsageFlags usage,
                      VkImageAspectFlags aspect, const std::string& kind,
                      const char* name) {
    Res r;
    r.kind = kind;
    r.format = format;
    r.aspect = aspect;
    VkImageCreateInfo ci{VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO};
    ci.imageType = VK_IMAGE_TYPE_2D;
    ci.format = format;
    ci.extent = {w ? w : 64, h ? h : 64, 1};
    ci.mipLevels = 1;
    ci.arrayLayers = 1;
    ci.samples = VK_SAMPLE_COUNT_1_BIT;
    ci.usage = usage;
    ci.initialLayout = VK_IMAGE_LAYOUT_UNDEFINED;
    VK(vkCreateImage(dev, &ci, 0, &r.img));
    VkMemoryRequirements mr;
    vkGetImageMemoryRequirements(dev, r.img, &mr);
    VkMemoryAllocateInfo ai{VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO};
    ai.allocationSize = mr.size;
    ai.memoryTypeIndex = memType(mr.memoryTypeBits, VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT);
    VK(vkAllocateMemory(dev, &ai, 0, &r.mem));
    vkBindImageMemory(dev, r.img, r.mem, 0);
    VkImageViewCreateInfo vi{VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO};
    vi.image = r.img;
    vi.viewType = VK_IMAGE_VIEW_TYPE_2D;
    vi.format = format;
    vi.subresourceRange = {aspect, 0, 1, 0, 1};
    VK(vkCreateImageView(dev, &vi, 0, &r.view));
    nameObj((uint64_t)r.img, VK_OBJECT_TYPE_IMAGE, name);
    return r;
}

static std::vector<char> readFile(const std::string& path) {
    std::ifstream f(path, std::ios::binary | std::ios::ate);
    if (!f) { printf("cannot open %s\n", path.c_str()); exit(1); }
    size_t n = (size_t)f.tellg();
    std::vector<char> c(n);
    f.seekg(0);
    f.read(c.data(), n);
    return c;
}

static VkShaderModule loadSM(const std::string& path) {
    auto c = readFile(path);
    VkShaderModuleCreateInfo ci{VK_STRUCTURE_TYPE_SHADER_MODULE_CREATE_INFO};
    ci.codeSize = c.size();
    ci.pCode = (const uint32_t*)c.data();
    VkShaderModule m;
    VK(vkCreateShaderModule(dev, &ci, 0, &m));
    return m;
}

static VkDescriptorType vkType(const std::string& t) {
    if (t == "storage_image") return VK_DESCRIPTOR_TYPE_STORAGE_IMAGE;
    if (t == "uniform_buffer") return VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER;
    if (t == "sampled_image") return VK_DESCRIPTOR_TYPE_SAMPLED_IMAGE;
    return VK_DESCRIPTOR_TYPE_STORAGE_BUFFER;
}

struct Pipe {
    VkPipeline pipe;
    VkPipelineLayout layout;
    VkDescriptorSetLayout dsl;
};

static Pipe makeCompute(const js::Value& binds, const std::string& spv) {
    Pipe r;
    std::vector<VkDescriptorSetLayoutBinding> bs;
    for (size_t i = 0; i < binds.size(); i++) {
        const js::Value& b = binds[i];
        VkDescriptorSetLayoutBinding lb{};
        lb.binding = (uint32_t)b["vk_binding"].asInt();
        lb.descriptorType = vkType(b["vk_dtype"].asStr());
        lb.descriptorCount = 1;
        lb.stageFlags = VK_SHADER_STAGE_COMPUTE_BIT;
        bs.push_back(lb);
    }
    VkDescriptorSetLayoutCreateInfo dlci{VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO};
    dlci.bindingCount = (uint32_t)bs.size();
    dlci.pBindings = bs.data();
    VK(vkCreateDescriptorSetLayout(dev, &dlci, 0, &r.dsl));
    VkPipelineLayoutCreateInfo plci{VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO};
    plci.setLayoutCount = 1;
    plci.pSetLayouts = &r.dsl;
    VK(vkCreatePipelineLayout(dev, &plci, 0, &r.layout));
    VkComputePipelineCreateInfo cpci{VK_STRUCTURE_TYPE_COMPUTE_PIPELINE_CREATE_INFO};
    cpci.stage.sType = VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO;
    cpci.stage.stage = VK_SHADER_STAGE_COMPUTE_BIT;
    cpci.stage.module = loadSM(spv);
    cpci.stage.pName = "main";
    cpci.layout = r.layout;
    VK(vkCreateComputePipelines(dev, 0, 1, &cpci, 0, &r.pipe));
    return r;
}

static void writeSet(VkDescriptorSet ds, const js::Value& binds) {
    std::vector<VkWriteDescriptorSet> ws;
    std::vector<VkDescriptorBufferInfo> bis(binds.size());
    std::vector<VkDescriptorImageInfo> iis(binds.size());
    for (size_t i = 0; i < binds.size(); i++) {
        const js::Value& b = binds[i];
        Res& res = g_res[b["res"].asStr()];
        VkWriteDescriptorSet w{VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET};
        w.dstSet = ds;
        w.dstBinding = (uint32_t)b["vk_binding"].asInt();
        w.descriptorCount = 1;
        w.descriptorType = vkType(b["vk_dtype"].asStr());
        if (w.descriptorType == VK_DESCRIPTOR_TYPE_STORAGE_IMAGE ||
            w.descriptorType == VK_DESCRIPTOR_TYPE_SAMPLED_IMAGE) {
            iis[i] = {0, res.view, VK_IMAGE_LAYOUT_GENERAL};
            w.pImageInfo = &iis[i];
        } else {
            bis[i] = {res.buf, 0, VK_WHOLE_SIZE};
            w.pBufferInfo = &bis[i];
        }
        ws.push_back(w);
    }
    if (!ws.empty())
        vkUpdateDescriptorSets(dev, (uint32_t)ws.size(), ws.data(), 0, 0);
}

static VkDescriptorPool g_gpool = 0;  // graphics (sampled-image) descriptors

// Record one graphics pass: render pass + framebuffer + pipeline (VS+PS) with the
// depth-stencil state implied by depth.access, then a fullscreen-triangle draw.
static void runGraphicsPass(const js::Value& p, const std::string& shaderDir) {
    const js::Value& colors = p["color"];
    const js::Value& depthJ = p["depth"];
    const js::Value& sample = p["sample"];
    const js::Value& vtx = p["vertex"];
    const js::Value& idxJ = p["index"];
    bool hasDepth = !depthJ.isNull();
    const uint32_t W = 64, H = 64;

    // input-assembler streams: one float4 attribute per vertex buffer, location i
    // (matching the VS's vk::location), each from its own binding (slot) i.
    std::vector<VkVertexInputBindingDescription> vibs;
    std::vector<VkVertexInputAttributeDescription> vias;
    for (size_t i = 0; i < vtx.size(); i++) {
        VkVertexInputBindingDescription b{};
        b.binding = (uint32_t)i;
        b.stride = 16;
        b.inputRate = VK_VERTEX_INPUT_RATE_VERTEX;
        vibs.push_back(b);
        VkVertexInputAttributeDescription a{};
        a.location = (uint32_t)i;
        a.binding = (uint32_t)i;
        a.format = VK_FORMAT_R32G32B32A32_SFLOAT;
        a.offset = 0;
        vias.push_back(a);
    }

    // ---- render pass ----
    std::vector<VkAttachmentDescription> atts;
    std::vector<VkAttachmentReference> colorRefs;
    VkAttachmentReference depthRef{};
    for (size_t i = 0; i < colors.size(); i++) {
        VkAttachmentDescription a{};
        a.format = VK_FORMAT_R8G8B8A8_UNORM;
        a.samples = VK_SAMPLE_COUNT_1_BIT;
        a.loadOp = VK_ATTACHMENT_LOAD_OP_DONT_CARE;
        a.storeOp = VK_ATTACHMENT_STORE_OP_STORE;
        a.stencilLoadOp = VK_ATTACHMENT_LOAD_OP_DONT_CARE;
        a.stencilStoreOp = VK_ATTACHMENT_STORE_OP_DONT_CARE;
        a.initialLayout = VK_IMAGE_LAYOUT_UNDEFINED;
        a.finalLayout = VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL;
        colorRefs.push_back({(uint32_t)atts.size(),
                             VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL});
        atts.push_back(a);
    }
    if (hasDepth) {
        VkAttachmentDescription a{};
        a.format = VK_FORMAT_D32_SFLOAT;
        a.samples = VK_SAMPLE_COUNT_1_BIT;
        a.loadOp = VK_ATTACHMENT_LOAD_OP_DONT_CARE;
        // depth.store: "dont_care" -> storeOp=DONT_CARE (RenderDoc reports a
        // Discard on the depth at the renderpass-END boundary); default STORE.
        a.storeOp = (depthJ["store"].asStr() == "dont_care")
                        ? VK_ATTACHMENT_STORE_OP_DONT_CARE
                        : VK_ATTACHMENT_STORE_OP_STORE;
        a.stencilLoadOp = VK_ATTACHMENT_LOAD_OP_DONT_CARE;
        a.stencilStoreOp = VK_ATTACHMENT_STORE_OP_DONT_CARE;
        a.initialLayout = VK_IMAGE_LAYOUT_UNDEFINED;
        a.finalLayout = VK_IMAGE_LAYOUT_DEPTH_STENCIL_ATTACHMENT_OPTIMAL;
        depthRef = {(uint32_t)atts.size(),
                    VK_IMAGE_LAYOUT_DEPTH_STENCIL_ATTACHMENT_OPTIMAL};
        atts.push_back(a);
    }
    VkSubpassDescription sub{};
    sub.pipelineBindPoint = VK_PIPELINE_BIND_POINT_GRAPHICS;
    sub.colorAttachmentCount = (uint32_t)colorRefs.size();
    sub.pColorAttachments = colorRefs.data();
    sub.pDepthStencilAttachment = hasDepth ? &depthRef : nullptr;
    VkRenderPassCreateInfo rpci{VK_STRUCTURE_TYPE_RENDER_PASS_CREATE_INFO};
    rpci.attachmentCount = (uint32_t)atts.size();
    rpci.pAttachments = atts.data();
    rpci.subpassCount = 1;
    rpci.pSubpasses = &sub;
    VkRenderPass rp;
    VK(vkCreateRenderPass(dev, &rpci, 0, &rp));

    // ---- framebuffer ----
    std::vector<VkImageView> views;
    for (size_t i = 0; i < colors.size(); i++)
        views.push_back(g_res[colors[i].asStr()].view);
    if (hasDepth) views.push_back(g_res[depthJ["res"].asStr()].view);
    VkFramebufferCreateInfo fbci{VK_STRUCTURE_TYPE_FRAMEBUFFER_CREATE_INFO};
    fbci.renderPass = rp;
    fbci.attachmentCount = (uint32_t)views.size();
    fbci.pAttachments = views.data();
    fbci.width = W;
    fbci.height = H;
    fbci.layers = 1;
    VkFramebuffer fb;
    VK(vkCreateFramebuffer(dev, &fbci, 0, &fb));

    // ---- descriptor set layout for PS sampled images ----
    std::vector<VkDescriptorSetLayoutBinding> bs;
    for (size_t i = 0; i < sample.size(); i++) {
        VkDescriptorSetLayoutBinding lb{};
        lb.binding = (uint32_t)sample[i]["vk_binding"].asInt();
        lb.descriptorType = VK_DESCRIPTOR_TYPE_SAMPLED_IMAGE;
        lb.descriptorCount = 1;
        lb.stageFlags = VK_SHADER_STAGE_FRAGMENT_BIT;
        bs.push_back(lb);
    }
    VkDescriptorSetLayout dsl;
    VkDescriptorSetLayoutCreateInfo dlci{VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO};
    dlci.bindingCount = (uint32_t)bs.size();
    dlci.pBindings = bs.data();
    VK(vkCreateDescriptorSetLayout(dev, &dlci, 0, &dsl));
    VkPipelineLayoutCreateInfo plci{VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO};
    plci.setLayoutCount = 1;
    plci.pSetLayouts = &dsl;
    VkPipelineLayout pl;
    VK(vkCreatePipelineLayout(dev, &plci, 0, &pl));

    // ---- pipeline ----
    VkShaderModule vs = loadSM(shaderDir + "/" + p["vs"].asStr() + ".spv");
    VkShaderModule ps = loadSM(shaderDir + "/" + p["ps"].asStr() + ".spv");
    VkPipelineShaderStageCreateInfo stages[2] = {};
    stages[0].sType = VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO;
    stages[0].stage = VK_SHADER_STAGE_VERTEX_BIT;
    stages[0].module = vs;
    stages[0].pName = "main";
    stages[1].sType = VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO;
    stages[1].stage = VK_SHADER_STAGE_FRAGMENT_BIT;
    stages[1].module = ps;
    stages[1].pName = "main";
    VkPipelineVertexInputStateCreateInfo vin{VK_STRUCTURE_TYPE_PIPELINE_VERTEX_INPUT_STATE_CREATE_INFO};
    vin.vertexBindingDescriptionCount = (uint32_t)vibs.size();
    vin.pVertexBindingDescriptions = vibs.empty() ? nullptr : vibs.data();
    vin.vertexAttributeDescriptionCount = (uint32_t)vias.size();
    vin.pVertexAttributeDescriptions = vias.empty() ? nullptr : vias.data();
    VkPipelineInputAssemblyStateCreateInfo ia{VK_STRUCTURE_TYPE_PIPELINE_INPUT_ASSEMBLY_STATE_CREATE_INFO};
    ia.topology = VK_PRIMITIVE_TOPOLOGY_TRIANGLE_LIST;
    VkViewport vp{0, 0, (float)W, (float)H, 0, 1};
    VkRect2D sci{{0, 0}, {W, H}};
    VkPipelineViewportStateCreateInfo vps{VK_STRUCTURE_TYPE_PIPELINE_VIEWPORT_STATE_CREATE_INFO};
    vps.viewportCount = 1;
    vps.pViewports = &vp;
    vps.scissorCount = 1;
    vps.pScissors = &sci;
    VkPipelineRasterizationStateCreateInfo rs{VK_STRUCTURE_TYPE_PIPELINE_RASTERIZATION_STATE_CREATE_INFO};
    rs.polygonMode = VK_POLYGON_MODE_FILL;
    rs.cullMode = VK_CULL_MODE_NONE;
    rs.frontFace = VK_FRONT_FACE_CLOCKWISE;
    rs.lineWidth = 1.0f;
    VkPipelineMultisampleStateCreateInfo ms{VK_STRUCTURE_TYPE_PIPELINE_MULTISAMPLE_STATE_CREATE_INFO};
    ms.rasterizationSamples = VK_SAMPLE_COUNT_1_BIT;
    VkPipelineDepthStencilStateCreateInfo ds{VK_STRUCTURE_TYPE_PIPELINE_DEPTH_STENCIL_STATE_CREATE_INFO};
    if (hasDepth) {
        std::string acc = depthJ["access"].asStr();
        ds.depthTestEnable = VK_TRUE;
        if (acc == "read") {
            ds.depthWriteEnable = VK_FALSE;
            ds.depthCompareOp = VK_COMPARE_OP_LESS_OR_EQUAL;
        } else if (acc == "rw") {
            ds.depthWriteEnable = VK_TRUE;
            ds.depthCompareOp = VK_COMPARE_OP_LESS_OR_EQUAL;
        } else {  // write: test always passes (no read), depth written
            ds.depthWriteEnable = VK_TRUE;
            ds.depthCompareOp = VK_COMPARE_OP_ALWAYS;
        }
    }
    std::vector<VkPipelineColorBlendAttachmentState> blends(colorRefs.size());
    for (auto& b : blends) b.colorWriteMask = 0xF;
    VkPipelineColorBlendStateCreateInfo cbs{VK_STRUCTURE_TYPE_PIPELINE_COLOR_BLEND_STATE_CREATE_INFO};
    cbs.attachmentCount = (uint32_t)blends.size();
    cbs.pAttachments = blends.data();
    VkGraphicsPipelineCreateInfo gp{VK_STRUCTURE_TYPE_GRAPHICS_PIPELINE_CREATE_INFO};
    gp.stageCount = 2;
    gp.pStages = stages;
    gp.pVertexInputState = &vin;
    gp.pInputAssemblyState = &ia;
    gp.pViewportState = &vps;
    gp.pRasterizationState = &rs;
    gp.pMultisampleState = &ms;
    gp.pDepthStencilState = hasDepth ? &ds : nullptr;
    gp.pColorBlendState = &cbs;
    gp.layout = pl;
    gp.renderPass = rp;
    gp.subpass = 0;
    VkPipeline pipe;
    VK(vkCreateGraphicsPipelines(dev, 0, 1, &gp, 0, &pipe));

    // ---- descriptor set for sampled SRVs ----
    VkDescriptorSet dset = 0;
    if (sample.size() > 0) {
        VkDescriptorSetAllocateInfo dsai{VK_STRUCTURE_TYPE_DESCRIPTOR_SET_ALLOCATE_INFO};
        dsai.descriptorPool = g_gpool;
        dsai.descriptorSetCount = 1;
        dsai.pSetLayouts = &dsl;
        VK(vkAllocateDescriptorSets(dev, &dsai, &dset));
        std::vector<VkWriteDescriptorSet> ws;
        std::vector<VkDescriptorImageInfo> iis(sample.size());
        for (size_t i = 0; i < sample.size(); i++) {
            Res& res = g_res[sample[i]["res"].asStr()];
            iis[i] = {0, res.view, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL};
            VkWriteDescriptorSet w{VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET};
            w.dstSet = dset;
            w.dstBinding = (uint32_t)sample[i]["vk_binding"].asInt();
            w.descriptorCount = 1;
            w.descriptorType = VK_DESCRIPTOR_TYPE_SAMPLED_IMAGE;
            w.pImageInfo = &iis[i];
            ws.push_back(w);
        }
        vkUpdateDescriptorSets(dev, (uint32_t)ws.size(), ws.data(), 0, 0);
    }

    // ---- record ----
    VkRenderPassBeginInfo rbi{VK_STRUCTURE_TYPE_RENDER_PASS_BEGIN_INFO};
    rbi.renderPass = rp;
    rbi.framebuffer = fb;
    rbi.renderArea = sci;
    vkCmdBeginRenderPass(cb, &rbi, VK_SUBPASS_CONTENTS_INLINE);
    vkCmdBindPipeline(cb, VK_PIPELINE_BIND_POINT_GRAPHICS, pipe);
    if (dset)
        vkCmdBindDescriptorSets(cb, VK_PIPELINE_BIND_POINT_GRAPHICS, pl, 0, 1, &dset, 0, 0);
    if (vtx.size() > 0) {
        std::vector<VkBuffer> vbs;
        std::vector<VkDeviceSize> offs;
        for (size_t i = 0; i < vtx.size(); i++) {
            vbs.push_back(g_res[vtx[i].asStr()].buf);
            offs.push_back(0);
        }
        vkCmdBindVertexBuffers(cb, 0, (uint32_t)vbs.size(), vbs.data(), offs.data());
        if (!idxJ.isNull()) {
            vkCmdBindIndexBuffer(cb, g_res[idxJ.asStr()].buf, 0, VK_INDEX_TYPE_UINT32);
            vkCmdDrawIndexed(cb, 3, 1, 0, 0, 0);
        } else {
            vkCmdDraw(cb, 3, 1, 0, 0);
        }
    } else {
        vkCmdDraw(cb, 3, 1, 0, 0);
    }
    vkCmdEndRenderPass(cb);
}

// marker stack management for nested scopes
static std::vector<std::string> g_open;
static void syncMarkers(const std::vector<std::string>& target) {
    size_t common = 0;
    while (common < g_open.size() && common < target.size() &&
           g_open[common] == target[common]) common++;
    while (g_open.size() > common) {
        if (endLabel) endLabel(cb);
        g_open.pop_back();
    }
    while (g_open.size() < target.size()) {
        const std::string& name = target[g_open.size()];
        VkDebugUtilsLabelEXT lab{VK_STRUCTURE_TYPE_DEBUG_UTILS_LABEL_EXT};
        lab.pLabelName = name.c_str();
        if (beginLabel) beginLabel(cb, &lab);
        g_open.push_back(name);
    }
}

// repeat>1 -> N distinct instance names ("Name 0".."Name N-1") so they form
// separate passes that bundling can re-merge.
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
        printf("usage: generic_vk <manifest.json> <shader_dir> <capture_template>\n");
        return 1;
    }
    std::string manifestPath = argv[1];
    std::string shaderDir = argv[2];
    std::string capTemplate = argv[3];

    js::Value M = js::parse_file(manifestPath);

    Rdoc rdoc;
    rdoc.init(capTemplate.c_str());

    const char* exts[] = {"VK_EXT_debug_utils"};
    VkApplicationInfo app{VK_STRUCTURE_TYPE_APPLICATION_INFO};
    app.apiVersion = VK_API_VERSION_1_1;
    VkInstanceCreateInfo ici{VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO};
    ici.pApplicationInfo = &app;
    ici.enabledExtensionCount = 1;
    ici.ppEnabledExtensionNames = exts;
    VkInstance inst;
    VK(vkCreateInstance(&ici, 0, &inst));
    beginLabel = (PFN_vkCmdBeginDebugUtilsLabelEXT)
        vkGetInstanceProcAddr(inst, "vkCmdBeginDebugUtilsLabelEXT");
    endLabel = (PFN_vkCmdEndDebugUtilsLabelEXT)
        vkGetInstanceProcAddr(inst, "vkCmdEndDebugUtilsLabelEXT");

    uint32_t n = 0;
    vkEnumeratePhysicalDevices(inst, &n, 0);
    std::vector<VkPhysicalDevice> pds(n);
    vkEnumeratePhysicalDevices(inst, &n, pds.data());
    pd = pds[0];
    uint32_t qn = 0;
    vkGetPhysicalDeviceQueueFamilyProperties(pd, &qn, 0);
    std::vector<VkQueueFamilyProperties> qp(qn);
    vkGetPhysicalDeviceQueueFamilyProperties(pd, &qn, qp.data());
    for (uint32_t i = 0; i < qn; i++)
        if (qp[i].queueFlags & VK_QUEUE_COMPUTE_BIT) { qf = i; break; }
    float pri = 1.0f;
    VkDeviceQueueCreateInfo qci{VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO};
    qci.queueFamilyIndex = qf;
    qci.queueCount = 1;
    qci.pQueuePriorities = &pri;
    VkDeviceCreateInfo dci{VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO};
    dci.queueCreateInfoCount = 1;
    dci.pQueueCreateInfos = &qci;
    VK(vkCreateDevice(pd, &dci, 0, &dev));
    vkGetDeviceQueue(dev, qf, 0, &queue);
    setName = (PFN_vkSetDebugUtilsObjectNameEXT)
        vkGetDeviceProcAddr(dev, "vkSetDebugUtilsObjectNameEXT");

    // resources
    const js::Value& resources = M["resources"];
    for (size_t i = 0; i < resources.size(); i++) {
        const js::Value& r = resources[i];
        std::string name = r["name"].asStr();
        std::string kind = r["kind"].asStr();
        if (kind == "buffer") {
            int elems = r["elements"].isNull() ? 256 : r["elements"].asInt();
            g_res[name] = makeBuf((VkDeviceSize)elems * 4, name.c_str());
        } else if (kind == "cbuffer") {
            int elems = r["elements"].isNull() ? 64 : r["elements"].asInt();
            g_res[name] = makeCBuf((VkDeviceSize)elems * 4, name.c_str());
        } else if (kind == "vbuffer") {
            g_res[name] = makeIABuf(sizeof(kVertsZero),
                VK_BUFFER_USAGE_VERTEX_BUFFER_BIT, kVertsZero, "vbuffer",
                name.c_str());
        } else if (kind == "ibuffer") {
            g_res[name] = makeIABuf(sizeof(kTriIndices),
                VK_BUFFER_USAGE_INDEX_BUFFER_BIT, kTriIndices, "ibuffer",
                name.c_str());
        } else if (kind == "uav_tex" || kind == "color" || kind == "depth" ||
                   kind == "sampled") {
            uint32_t w = 64, h = 64;
            if (!r["dims"].isNull() && r["dims"].size() >= 2) {
                w = (uint32_t)r["dims"][0].asInt();
                h = (uint32_t)r["dims"][1].asInt();
            }
            if (kind == "uav_tex") {
                g_res[name] = makeImg(w, h, name.c_str());
            } else if (kind == "color") {
                g_res[name] = makeImage2(w, h, VK_FORMAT_R8G8B8A8_UNORM,
                    VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT | VK_IMAGE_USAGE_SAMPLED_BIT,
                    VK_IMAGE_ASPECT_COLOR_BIT, "color", name.c_str());
            } else if (kind == "depth") {
                g_res[name] = makeImage2(w, h, VK_FORMAT_D32_SFLOAT,
                    VK_IMAGE_USAGE_DEPTH_STENCIL_ATTACHMENT_BIT,
                    VK_IMAGE_ASPECT_DEPTH_BIT, "depth", name.c_str());
            } else {  // sampled
                g_res[name] = makeImage2(w, h, VK_FORMAT_R8G8B8A8_UNORM,
                    VK_IMAGE_USAGE_SAMPLED_BIT | VK_IMAGE_USAGE_TRANSFER_DST_BIT,
                    VK_IMAGE_ASPECT_COLOR_BIT, "sampled", name.c_str());
            }
        }
        // swapchain: later phases
    }

    // descriptor pool sized from all compute binds
    uint32_t nBuf = 0, nImg = 0, nUbo = 0, nSets = 0;
    const js::Value& passes = M["passes"];
    for (size_t i = 0; i < passes.size(); i++) {
        const js::Value& p = passes[i];
        if (p["type"].asStr() != "compute") continue;
        int rep = p["repeat"].isNull() ? 1 : p["repeat"].asInt();
        if (rep < 1) rep = 1;
        nSets += rep;
        const js::Value& binds = p["binds"];
        for (size_t j = 0; j < binds.size(); j++) {
            VkDescriptorType dt = vkType(binds[j]["vk_dtype"].asStr());
            if (dt == VK_DESCRIPTOR_TYPE_STORAGE_IMAGE) nImg += rep;
            else if (dt == VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER) nUbo += rep;
            else nBuf += rep;
        }
    }
    VkDescriptorPoolSize psz[] = {
        {VK_DESCRIPTOR_TYPE_STORAGE_BUFFER, nBuf ? nBuf : 1},
        {VK_DESCRIPTOR_TYPE_STORAGE_IMAGE, nImg ? nImg : 1},
        {VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER, nUbo ? nUbo : 1}};
    VkDescriptorPoolCreateInfo dpci{VK_STRUCTURE_TYPE_DESCRIPTOR_POOL_CREATE_INFO};
    dpci.maxSets = nSets ? nSets : 1;
    dpci.poolSizeCount = 3;
    dpci.pPoolSizes = psz;
    VK(vkCreateDescriptorPool(dev, &dpci, 0, &dpool));

    // graphics (sampled-image) descriptor pool
    uint32_t nSamp = 0, nGfx = 0;
    for (size_t i = 0; i < passes.size(); i++) {
        if (passes[i]["type"].asStr() != "graphics") continue;
        nGfx++;
        nSamp += (uint32_t)passes[i]["sample"].size();
    }
    VkDescriptorPoolSize gsz = {VK_DESCRIPTOR_TYPE_SAMPLED_IMAGE, nSamp ? nSamp : 1};
    VkDescriptorPoolCreateInfo gpci{VK_STRUCTURE_TYPE_DESCRIPTOR_POOL_CREATE_INFO};
    gpci.maxSets = nGfx ? nGfx : 1;
    gpci.poolSizeCount = 1;
    gpci.pPoolSizes = &gsz;
    VK(vkCreateDescriptorPool(dev, &gpci, 0, &g_gpool));

    // build a pipeline per compute pass
    std::vector<Pipe> pipes(passes.size());
    for (size_t i = 0; i < passes.size(); i++) {
        const js::Value& p = passes[i];
        if (p["type"].asStr() != "compute") continue;
        std::string spv = shaderDir + "/" + p["cs"].asStr() + ".spv";
        pipes[i] = makeCompute(p["binds"], spv);
    }

    VkCommandPoolCreateInfo cmpci{VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO};
    cmpci.queueFamilyIndex = qf;
    VkCommandPool cp;
    VK(vkCreateCommandPool(dev, &cmpci, 0, &cp));
    VkCommandBufferAllocateInfo cbai{VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO};
    cbai.commandPool = cp;
    cbai.level = VK_COMMAND_BUFFER_LEVEL_PRIMARY;
    cbai.commandBufferCount = 1;
    VK(vkAllocateCommandBuffers(dev, &cbai, &cb));

    rdoc.begin();
    VkCommandBufferBeginInfo cbi{VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO};
    VK(vkBeginCommandBuffer(cb, &cbi));

    // transition uav_tex -> GENERAL, sampled -> SHADER_READ_ONLY
    for (auto& kv : g_res) {
        Res& res = kv.second;
        if (res.kind != "uav_tex" && res.kind != "sampled") continue;
        VkImageMemoryBarrier ib{VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER};
        ib.oldLayout = VK_IMAGE_LAYOUT_UNDEFINED;
        ib.newLayout = res.kind == "uav_tex" ? VK_IMAGE_LAYOUT_GENERAL
                                             : VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL;
        ib.image = res.img;
        ib.subresourceRange = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1};
        ib.dstAccessMask = VK_ACCESS_SHADER_READ_BIT | VK_ACCESS_SHADER_WRITE_BIT;
        vkCmdPipelineBarrier(cb, VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT,
                             VK_PIPELINE_STAGE_ALL_GRAPHICS_BIT, 0, 0, 0, 0, 0, 1, &ib);
    }

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
            syncMarkers(target);

            if (type == "graphics") {
                runGraphicsPass(p, shaderDir);
                continue;
            }
            if (type == "transfer") {
                const js::Value& copy = p["copy"];
                VkBufferCopy region{0, 0, 16};  // size irrelevant to the graph
                vkCmdCopyBuffer(cb, g_res[copy["src"].asStr()].buf,
                                g_res[copy["dst"].asStr()].buf, 1, &region);
                continue;
            }

            VkDescriptorSetAllocateInfo dsai{VK_STRUCTURE_TYPE_DESCRIPTOR_SET_ALLOCATE_INFO};
            dsai.descriptorPool = dpool;
            dsai.descriptorSetCount = 1;
            dsai.pSetLayouts = &pipes[i].dsl;
            VkDescriptorSet ds;
            VK(vkAllocateDescriptorSets(dev, &dsai, &ds));
            writeSet(ds, p["binds"]);

            vkCmdBindPipeline(cb, VK_PIPELINE_BIND_POINT_COMPUTE, pipes[i].pipe);
            vkCmdBindDescriptorSets(cb, VK_PIPELINE_BIND_POINT_COMPUTE,
                                    pipes[i].layout, 0, 1, &ds, 0, 0);
            int groups = p["groups"].isNull() ? 1 : p["groups"].asInt();
            vkCmdDispatch(cb, groups ? groups : 1, 1, 1);

            VkMemoryBarrier mb{VK_STRUCTURE_TYPE_MEMORY_BARRIER};
            mb.srcAccessMask = VK_ACCESS_SHADER_WRITE_BIT;
            mb.dstAccessMask = VK_ACCESS_SHADER_READ_BIT | VK_ACCESS_SHADER_WRITE_BIT;
            vkCmdPipelineBarrier(cb, VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT,
                                 VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT, 0, 1, &mb, 0, 0, 0, 0);
        }
    }
    syncMarkers({});  // close all markers

    VK(vkEndCommandBuffer(cb));
    VkSubmitInfo si{VK_STRUCTURE_TYPE_SUBMIT_INFO};
    si.commandBufferCount = 1;
    si.pCommandBuffers = &cb;
    VK(vkQueueSubmit(queue, 1, &si, 0));
    VK(vkQueueWaitIdle(queue));
    rdoc.end();

    printf("generic_vk done: %s\n", M["name"].asStr().c_str());
    return 0;
}
