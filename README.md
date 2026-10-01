# Video URL to Chinese transcript

[简体中文](README_zh.md)

A Windows command line workflow that downloads media with the local cat-catch CLI, extracts audio with FFmpeg, transcribes it with the local Whisper service, and corrects clear Chinese ASR errors with the local llama service. Raw Whisper output is always kept separately.

## Requirements

- Python 3.10+, Node.js 22+, and FFmpeg on `PATH`.
- The `cat-catch`, `whisper.cpp`, and `llama.cpp` Git submodules under `third_party/`. Clone with `--recurse-submodules`.
- The local `whisper-agent` and `llama-agent` bridge services, each with its configured Python environment and `cli.py`. These machine-specific bridges and model files are **not** part of the upstream source submodules. The Whisper bridge must permit reading this project's output directory.

The cat-catch default points to its submodule. The bridge defaults match the author's Windows machine (`~/Documents/ai-services/whisper-agent` and `llama-agent`); use the directory options below elsewhere. Only media that cat-catch can access is supported; DRM decryption is unavailable.

## Setup

```powershell
git clone --recurse-submodules https://github.com/Jiusi-pys/ExtractTextFromVideoUrl.git
cd ExtractTextFromVideoUrl
Push-Location third_party/cat-catch
npm.cmd ci
npx.cmd playwright install chromium
Pop-Location
python -m unittest discover -s tests -v
```

If you already cloned the repository, run `git submodule update --init --recursive`. Install/configure the two local bridge services separately and point `--whisper-agent-dir` and `--llama-agent-dir` to them. The submodules pin source revisions but do not include compiled executables or models.

## Run

```powershell
python transcript_pipeline.py "https://example.com/watch"
python transcript_pipeline.py "https://example.com/watch" --selection manual
python transcript_pipeline.py "https://example.com/watch" --selection manual --resource-id 2
python transcript_pipeline.py "https://example.com/video.mp4"
```

Auto selection is the default. It tries the largest reported complete video or audio resource first, then other candidates if extraction finds no audio. When sizes are unavailable, an HLS/DASH manifest takes priority. Manual mode lists captured resources and prompts for an ID; without an interactive terminal, supply `--resource-id`. An explicit ID is also accepted in auto mode and disables fallback.

Useful options:

| Option | Purpose |
| --- | --- |
| `--output-dir PATH` | Parent directory for unique run folders. |
| `--cat-catch-dir PATH` | cat-catch checkout; defaults to `third_party/cat-catch`. |
| `--whisper-agent-dir PATH` | Local Whisper service directory. |
| `--llama-agent-dir PATH` | Local llama correction service directory. |
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
