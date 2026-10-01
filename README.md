# Video URL to Chinese transcript

[简体中文](README_zh.md)

A Windows command line workflow that downloads media with the local cat-catch CLI, extracts audio with FFmpeg, transcribes it with the local Whisper service, and corrects clear Chinese ASR errors with the local llama service. Raw Whisper output is always kept separately.

## Requirements

- Windows, Git, Python 3.10+, Node.js 22+, FFmpeg, CMake, and a Visual Studio C++ build toolchain.
- Windows 11 Pro with an AMD GPU and current AMD graphics drivers. Install the LunarG Vulkan SDK (including `Bin/glslc.exe`) for the default GPU build; `-CpuOnly` is available for troubleshooting.
- The three pinned upstream source repositories are included as Git submodules. This repository also contains the Python bridge code for both model servers.

Model weights and compiled binaries are generated locally and ignored by Git. The default llama quality profile downloads a large model from Hugging Face on first use. Only media that cat-catch can access is supported; DRM decryption is unavailable.

## Setup

```powershell
git clone --recurse-submodules https://github.com/Jiusi-pys/ExtractTextFromVideoUrl.git
cd ExtractTextFromVideoUrl
.\setup.ps1
```

The setup script creates the Python environment, installs cat-catch and Chromium, builds both local server executables with Vulkan enabled for AMD GPUs, prepares local configs, and downloads the Whisper model. It builds BoringSSL into llama.cpp so the server can download its model over HTTPS. Install the AMD Vulkan driver and LunarG Vulkan SDK before running `.\setup.ps1`; llama.cpp uses `Vulkan0` and GPU layers by default. Use `.\setup.ps1 -CpuOnly` for a CPU build. To only prepare Python and Node dependencies, use `.\setup.ps1 -SkipNativeBuild -SkipModelDownload`, then edit `local_services/*/config/config.json` to point to existing servers and models.

The Whisper model is about 574 MB. The llama quality model can require substantial disk space, memory, and download time; use `--llama-profile smoke` on a smaller machine. Local settings go in ignored `config.json` files. Edit the `profiles`, server paths, or device settings there as needed. Setup preserves custom model profiles while updating compiled-server paths and device settings when it builds.

## Run

```powershell
.\.venv\Scripts\python.exe .\transcript_pipeline.py "https://example.com/watch"
.\.venv\Scripts\python.exe .\transcript_pipeline.py "https://example.com/watch" --selection manual
.\.venv\Scripts\python.exe .\transcript_pipeline.py "https://example.com/watch" --selection manual --resource-id 2
.\.venv\Scripts\python.exe .\transcript_pipeline.py "https://example.com/video.mp4"
```

Auto selection is the default. It tries the largest reported complete video or audio resource first, then other candidates if extraction finds no audio. When sizes are unavailable, an HLS/DASH manifest takes priority. Manual mode lists captured resources and prompts for an ID; without an interactive terminal, supply `--resource-id`. An explicit ID is also accepted in auto mode and disables fallback.

Useful options:

| Option | Purpose |
| --- | --- |
| `--output-dir PATH` | Parent directory for unique run folders. |
| `--cat-catch-dir PATH` | cat-catch checkout; defaults to `third_party/cat-catch`. |
| `--whisper-service-dir PATH` | Bundled Whisper service and local config directory. |
| `--llama-service-dir PATH` | Bundled llama service and local config directory. |
| `--cookies PATH` or `--storage-state PATH` | Authenticate webpage capture with a Playwright cookie/state JSON file. |
| `--click CSS` | Click a page element before capture; repeatable. |
| `--wait SECONDS` | Webpage capture time, default 15 seconds. |
| `--duration SECONDS` | Required for live HLS/DASH streams. |
| `--whisper-mode {quality,balanced,fast}` | Whisper mode; default `quality`. |
| `--llama-profile NAME` | Correction profile; default `quality`. |

Each successful run writes `source-*`, `audio.wav`, `raw.json`, `raw.txt`, `raw.srt`, `corrected.txt`, `corrections.json`, and `run.json`. The command prints the corrected text path. URL query strings are omitted from run metadata, and temporary capture data is removed after downloading. If a later stage fails, completed artifacts stay in the run folder. On candidate failures, `attempts.json` records the reasons.

The correction service only applies high-confidence text edits. It does not intentionally summarize or rewrite the speech. Always check names and quotations against the audio when accuracy matters.

## Tests

```powershell
python -m unittest discover -s tests -v
```
