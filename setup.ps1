param(
    [switch]$CpuOnly,
    [switch]$SkipNativeBuild,
    [switch]$SkipModelDownload
)

$ErrorActionPreference = 'Stop'
$useVulkan = -not $CpuOnly
$repo = $PSScriptRoot
Set-Location $repo

foreach ($tool in @('python', 'node', 'npm', 'npx', 'ffmpeg', 'cmake')) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        if ($tool -eq 'cmake' -and $SkipNativeBuild) { continue }
        throw "Required command not found on PATH: $tool"
    }
}

foreach ($submodule in @('cat-catch', 'whisper.cpp', 'llama.cpp')) {
    if (-not (Test-Path (Join-Path $repo "third_party/$submodule/.git"))) {
        throw "Missing submodule third_party/$submodule. Run: git submodule update --init --recursive"
    }
}

$venvPython = Join-Path $repo '.venv/Scripts/python.exe'
if (-not (Test-Path $venvPython)) {
    python -m venv (Join-Path $repo '.venv')
}
& $venvPython -m pip install -r (Join-Path $repo 'requirements.txt')

$catDir = Join-Path $repo 'third_party/cat-catch'
Push-Location $catDir
try {
    npm.cmd ci
    npx.cmd playwright install chromium
} finally {
    Pop-Location
}

foreach ($service in @('whisper', 'llama')) {
    $serviceDir = Join-Path $repo "local_services/$service"
    $configDir = Join-Path $serviceDir 'config'
    $configFile = Join-Path $configDir 'config.json'
    $templateFile = Join-Path $configDir 'config.example.json'
    if (-not (Test-Path $configFile)) {
        Copy-Item $templateFile $configFile
    }
}

New-Item -ItemType Directory -Force -Path (Join-Path $repo 'models/whisper'), (Join-Path $repo '.local'), (Join-Path $repo 'outputs') | Out-Null

if (-not $SkipNativeBuild) {
    if ($useVulkan) {
        $sdkRoot = $env:VULKAN_SDK
        $glslc = if ($sdkRoot) { Join-Path $sdkRoot 'Bin/glslc.exe' } else { $null }
        if (-not $sdkRoot -or -not (Test-Path $glslc)) {
            throw 'AMD GPU acceleration requires the LunarG Vulkan SDK. Install it, verify VULKAN_SDK and its Bin/glslc.exe are available, or run .\setup.ps1 -CpuOnly.'
        }
    }
    $vulkan = if ($useVulkan) { 'ON' } else { 'OFF' }
    $whisperSource = Join-Path $repo 'third_party/whisper.cpp'
    $whisperBuild = Join-Path $repo 'build/whisper'
    cmake -S $whisperSource -B $whisperBuild -DWHISPER_BUILD_SERVER=ON "-DGGML_VULKAN=$vulkan"
    if ($LASTEXITCODE -ne 0) { throw 'Whisper CMake configure failed.' }
    cmake --build $whisperBuild --config Release --target whisper-server --parallel
    if ($LASTEXITCODE -ne 0) { throw 'Whisper server build failed.' }

    $llamaSource = Join-Path $repo 'third_party/llama.cpp'
    $llamaBuild = Join-Path $repo 'build/llama'
    cmake -S $llamaSource -B $llamaBuild "-DGGML_VULKAN=$vulkan" -DLLAMA_BUILD_BORINGSSL=ON
    if ($LASTEXITCODE -ne 0) { throw 'llama.cpp CMake configure failed.' }
    cmake --build $llamaBuild --config Release --target llama-server --parallel
    if ($LASTEXITCODE -ne 0) { throw 'llama-server build failed.' }

    $whisperExe = Get-ChildItem $whisperBuild -Filter 'whisper-server.exe' -File -Recurse | Select-Object -First 1
    $llamaExe = Get-ChildItem $llamaBuild -Filter 'llama-server.exe' -File -Recurse | Select-Object -First 1
    if (-not $whisperExe -or -not $llamaExe) { throw 'Build completed but a server executable was not found.' }

    $whisperConfigPath = Join-Path $repo 'local_services/whisper/config/config.json'
    $whisperConfig = Get-Content $whisperConfigPath -Raw | ConvertFrom-Json
    $whisperConfig.server_exe = $whisperExe.FullName
    $whisperConfig.gpu_device = 0
    $encoding = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllText($whisperConfigPath, ($whisperConfig | ConvertTo-Json -Depth 12), $encoding)

    $llamaConfigPath = Join-Path $repo 'local_services/llama/config/config.json'
    $llamaConfig = Get-Content $llamaConfigPath -Raw | ConvertFrom-Json
    $llamaConfig.server_exe = $llamaExe.FullName
    $llamaConfig.device = if ($useVulkan) { 'Vulkan0' } else { '' }
    $gpuLayers = if ($useVulkan) { 999 } else { 0 }
    if ($null -eq $llamaConfig.PSObject.Properties['n_gpu_layers']) {
        $llamaConfig | Add-Member -NotePropertyName n_gpu_layers -NotePropertyValue $gpuLayers
    } else {
        $llamaConfig.n_gpu_layers = $gpuLayers
    }
    [System.IO.File]::WriteAllText($llamaConfigPath, ($llamaConfig | ConvertTo-Json -Depth 12), $encoding)
}

if (-not $SkipModelDownload) {
    $model = Join-Path $repo 'models/whisper/ggml-large-v3-turbo-q5_0.bin'
    if (-not (Test-Path $model)) {
        & (Join-Path $repo 'third_party/whisper.cpp/models/download-ggml-model.cmd') 'large-v3-turbo-q5_0' (Join-Path $repo 'models/whisper')
        if ($LASTEXITCODE -ne 0) { throw 'Whisper model download failed.' }
    }
}

Write-Host 'Setup complete. Run: .\.venv\Scripts\python.exe .\transcript_pipeline.py <url>'
if ($useVulkan) { Write-Host 'AMD GPU mode: Vulkan (device Vulkan0, llama GPU layers enabled).' }
else { Write-Host 'CPU-only mode selected.' }
