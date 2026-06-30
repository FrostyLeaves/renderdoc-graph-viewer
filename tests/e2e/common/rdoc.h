// Headless RenderDoc in-application capture: load renderdoc.dll, bracket one
// frame with StartFrameCapture/EndFrameCapture (device+window NULL = whole
// frame, no swapchain needed), write a .rdc to the given path template.
#pragma once
#include <windows.h>
#include <cstdio>
#include "renderdoc_app.h"

struct Rdoc {
    RENDERDOC_API_1_4_0* api = nullptr;

    bool init(const char* path_template) {
        // already loaded when running under RenderDoc, otherwise resolved on PATH
        HMODULE m = LoadLibraryA("renderdoc.dll");
        if (!m) { printf("[rdoc] renderdoc.dll not found (run under RenderDoc or put it on PATH)\n"); return false; }
        pRENDERDOC_GetAPI get =
            (pRENDERDOC_GetAPI)GetProcAddress(m, "RENDERDOC_GetAPI");
        if (!get || get(eRENDERDOC_API_Version_1_4_0, (void**)&api) != 1) {
            printf("[rdoc] RENDERDOC_GetAPI failed\n");
            api = nullptr;
            return false;
        }
        api->SetCaptureFilePathTemplate(path_template);
        return true;
    }
    void begin() { if (api) api->StartFrameCapture(NULL, NULL); }
    void end() {
        if (api) {
            uint32_t ok = api->EndFrameCapture(NULL, NULL);
            printf("[rdoc] EndFrameCapture -> %u\n", ok);
        }
    }
};
