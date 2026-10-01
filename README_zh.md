# 从视频网址生成中文文稿

[English](README.md)

这是一个 Windows 命令行工具：用 cat-catch 下载网页或直链媒体，用 FFmpeg 提取音频，用本机 Whisper 服务转写，再用本机 llama 服务纠正明确的中文识别错误。原始 Whisper 结果始终单独保留。

## 依赖与安装

- Python 3.10+、Node.js 22+，以及已加入 `PATH` 的 FFmpeg。
- `third_party/` 下的 cat-catch、whisper.cpp、llama.cpp Git 子模块。
- 已配置的本机 `whisper-agent` 和 `llama-agent` 桥接服务及其 Python 环境、`cli.py`。这些桥接程序和模型文件不属于上游源码子模块；Whisper 桥接服务须允许读取本项目的输出目录。

```powershell
git clone --recurse-submodules https://github.com/Jiusi-pys/ExtractTextFromVideoUrl.git
cd ExtractTextFromVideoUrl
Push-Location third_party/cat-catch
npm.cmd ci
npx.cmd playwright install chromium
Pop-Location
python -m unittest discover -s tests -v
```

已有仓库可执行 `git submodule update --init --recursive`。cat-catch 默认使用子模块；Whisper 和 llama 桥接服务默认路径是作者电脑上的 `~/Documents/ai-services/whisper-agent` 与 `llama-agent`，其他电脑请用参数指定。子模块固定了源码版本，不包含编译产物或模型。只支持 cat-catch 可访问的媒体，不支持 DRM 解密。

## 使用

```powershell
python transcript_pipeline.py "https://example.com/watch"
python transcript_pipeline.py "https://example.com/watch" --selection manual
python transcript_pipeline.py "https://example.com/watch" --selection manual --resource-id 2
python transcript_pipeline.py "https://example.com/video.mp4"
```

默认是 `auto`：优先尝试报告大小最大的完整视频或音频资源；提取不到音轨时依次尝试其他候选。没有可用大小时优先 HLS/DASH 清单。`manual` 会列出资源并让用户输入 ID；无交互终端时必须指定 `--resource-id`。显式指定 ID 也可用于自动模式，并会停用候选回退。

| 参数 | 用途 |
| --- | --- |
| `--output-dir PATH` | 每次运行输出目录的父目录。 |
| `--cat-catch-dir PATH` | cat-catch 目录，默认 `third_party/cat-catch`。 |
| `--whisper-agent-dir PATH` | 本机 Whisper 桥接服务目录。 |
| `--llama-agent-dir PATH` | 本机 llama 桥接服务目录。 |
| `--cookies PATH` 或 `--storage-state PATH` | 网页嗅探时导入 Playwright Cookie 或会话状态。 |
| `--click CSS` | 嗅探前点击网页元素，可重复使用。 |
| `--wait SECONDS` | 网页嗅探时长，默认 15 秒。 |
| `--duration SECONDS` | 直播 HLS/DASH 必须指定的时长。 |
| `--whisper-mode {quality,balanced,fast}` | Whisper 模式，默认 `quality`。 |
| `--llama-profile NAME` | 纠错配置，默认 `quality`。 |

成功后，每次运行会保存下载的 `source-*`、`audio.wav`、原始 `raw.json`／`raw.txt`／`raw.srt`、`corrected.txt`、`corrections.json` 和 `run.json`，并在终端输出校正文稿路径。运行记录会去除网址查询参数，临时嗅探文件会在下载后删除。后续步骤失败时，已完成的文件仍会保留；候选失败原因写入 `attempts.json`。

纠错服务仅应用高置信度文本修改，不以摘要或改写为目标。专名与引文仍应对照音频核查。

## 测试

```powershell
python -m unittest discover -s tests -v
```
