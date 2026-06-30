@echo off
REM Build the generic per-API runtime exes once (manifest-driven). Swapping a
REM scene only re-runs gen_all.py + the exe, never this. Run from anywhere.
cd /d "%~dp0"
REM VCVARS / VK can be overridden via environment; defaults match a typical install
if not defined VCVARS set "VCVARS=C:\Program Files\Microsoft Visual Studio\18\Community\VC\Auxiliary\Build\vcvars64.bat"
if not defined VK set "VK=C:\VulkanSDK\1.4.313.2"
call "%VCVARS%" >nul
if not exist _build mkdir _build

cl /nologo /EHsc /std:c++17 /I"%VK%\Include" /Icommon runtime\vk\generic_vk.cpp ^
   /Fe:_build\generic_vk.exe /Fo:_build\ ^
   /link /LIBPATH:"%VK%\Lib" vulkan-1.lib || exit /b 1

cl /nologo /EHsc /std:c++17 /Icommon runtime\d3d11\generic_d3d11.cpp ^
   /Fe:_build\generic_d3d11.exe /Fo:_build\ ^
   /link d3d11.lib dxguid.lib || exit /b 1

cl /nologo /EHsc /std:c++17 /Icommon runtime\d3d12\generic_d3d12.cpp ^
   /Fe:_build\generic_d3d12.exe /Fo:_build\ ^
   /link d3d12.lib dxgi.lib dxguid.lib user32.lib || exit /b 1

echo BUILD_OK
