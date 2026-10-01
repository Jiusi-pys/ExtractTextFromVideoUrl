# 从视频网址生成中文文稿

[English](README.md)

这是一个 Windows 命令行工具：用 cat-catch 下载网页或直链媒体，用 FFmpeg 提取音频，用本机 Whisper 服务转写，再用本机 llama 服务纠正明确的中文识别错误。原始 Whisper 结果始终单独保留。

## 依赖与安装

- Python 3.10+、Node.js 22+，以及已加入 `PATH` 的 FFmpeg。
- CMake 和 Visual Studio C++ 工具链；使用 Vulkan 加速还需 Vulkan SDK 与兼容的 GPU 驱动。
- `third_party/` 下的 cat-catch、whisper.cpp、llama.cpp Git 子模块。
- 仓库自带 Whisper 与 llama Python 桥接代码。模型权重和编译后的程序在本机生成，不会提交到 Git。

```powershell
git clone --recurse-submodules https://github.com/Jiusi-pys/ExtractTextFromVideoUrl.git
cd ExtractTextFromVideoUrl
.\setup.ps1
```

安装脚本会创建 Python 环境、安装 cat-catch 与 Chromium、编译两个服务端程序、准备本机配置，并下载 Whisper 模型。它会将 BoringSSL 编入 llama.cpp，使服务端能通过 HTTPS 下载模型。使用 Vulkan 加速时先安装 Vulkan SDK，再运行 `.\setup.ps1 -UseVulkan`。只准备 Python/Node 依赖而不编译或下载模型，可运行 `.\setup.ps1 -SkipNativeBuild -SkipModelDownload`；之后需编辑 `local_services/*/config/config.json` 指定已有服务程序与模型路径。

Whisper 模型约 574 MB。默认 llama `quality` 配置会在首次运行时从 Hugging Face 下载较大的模型；硬件资源有限时可用 `--llama-profile smoke`。本机配置文件已加入 Git 忽略规则。重新编译时安装脚本会更新服务端程序路径和设备设置，并保留模型配置。只支持 cat-catch 可访问的媒体，不支持 DRM 解密。

## 使用

```powershell
.\.venv\Scripts\python.exe .\transcript_pipeline.py "https://example.com/watch"
.\.venv\Scripts\python.exe .\transcript_pipeline.py "https://example.com/watch" --selection manual
.\.venv\Scripts\python.exe .\transcript_pipeline.py "https://example.com/watch" --selection manual --resource-id 2
.\.venv\Scripts\python.exe .\transcript_pipeline.py "https://example.com/video.mp4"
```

默认是 `auto`：优先尝试报告大小最大的完整视频或音频资源；提取不到音轨时依次尝试其他候选。没有可用大小时优先 HLS/DASH 清单。`manual` 会列出资源并让用户输入 ID；无交互终端时必须指定 `--resource-id`。显式指定 ID 也可用于自动模式，并会停用候选回退。

| 参数 | 用途 |
| --- | --- |
| `--output-dir PATH` | 每次运行输出目录的父目录。 |
| `--cat-catch-dir PATH` | cat-catch 目录，默认 `third_party/cat-catch`。 |
| `--whisper-service-dir PATH` | 随仓库提供的 Whisper 服务目录及本机配置目录。 |
| `--llama-service-dir PATH` | 随仓库提供的 llama 服务目录及本机配置目录。 |
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
