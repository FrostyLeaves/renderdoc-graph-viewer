@echo off
REM Build the generic per-API runtime exes once (manifest-driven). Swapping a
REM scene only re-runs gen_all.py + the exe, never this. Run from anywhere.
cd /d "%~dp0"
REM VK from the Vulkan SDK installer's VULKAN_SDK (override via the VK env var).
if not defined VK set "VK=%VULKAN_SDK%"
if not defined VK echo ERROR: Vulkan SDK not found -- set VULKAN_SDK or VK & exit /b 1
REM VCVARS via vswhere, the canonical VS locator (override via the VCVARS env var).
if not defined VCVARS for /f "usebackq tokens=*" %%i in (`"%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe" -latest -property installationPath 2^>nul`) do set "VCVARS=%%i\VC\Auxiliary\Build\vcvars64.bat"
if not defined VCVARS echo ERROR: Visual Studio not found via vswhere -- set VCVARS & exit /b 1
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
