import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from transcript_pipeline import (
    PipelineError,
    choose_candidates,
    choose_manual_resource,
    is_direct_media_url,
    split_text,
    run_pipeline,
    _invoke,
    _safe_url,
    _correct_chunk,
)


RESOURCES = [
    {"id": 1, "category": "video", "size": 10, "url": "https://x.test/ad.mp4"},
    {"id": 2, "category": "video", "size": 100, "url": "https://x.test/main.mp4"},
    {"id": 3, "category": "audio", "size": 80, "url": "https://x.test/main.m4a"},
]


class SelectionTests(unittest.TestCase):
    def test_direct_media_url(self):
        self.assertTrue(is_direct_media_url("https://x.test/master.m3u8?token=1"))
        self.assertTrue(is_direct_media_url("https://x.test/video.mp4"))
        self.assertFalse(is_direct_media_url("https://x.test/watch?v=1"))

    def test_signed_url_is_redacted_in_metadata(self):
        self.assertEqual(_safe_url("https://x.test/video.mp4?token=secret#part"), "https://x.test/video.mp4")

    def test_auto_largest_first_and_audio_fallback(self):
        self.assertEqual([r["id"] for r in choose_candidates(RESOURCES)], [2, 3, 1])

    def test_unknown_sizes_prefer_manifest(self):
        resources = [
            {"id": 1, "category": "video", "size": None, "url": "https://x.test/chunk.ts"},
            {"id": 2, "category": "hls", "size": None, "url": "https://x.test/master.m3u8"},
        ]
        self.assertEqual(choose_candidates(resources)[0]["id"], 2)

    def test_manual_id_and_invalid_id(self):
        self.assertEqual(choose_manual_resource(RESOURCES, 3, False)["id"], 3)
        with self.assertRaisesRegex(PipelineError, "Resource ID 9"):
            choose_manual_resource(RESOURCES, 9, False)
        with self.assertRaisesRegex(PipelineError, "--resource-id"):
            choose_manual_resource(RESOURCES, None, False)

    def test_manual_prompt(self):
        with patch("builtins.input", return_value="2"):
            self.assertEqual(choose_manual_resource(RESOURCES, None, True)["id"], 2)


class CorrectionTests(unittest.TestCase):
    def test_correction_retry_is_bounded_and_stage_errors_are_not_retried(self):
        calls = []
        def fail(args, **kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args, 1, "", "JSONDecodeError: truncated response")
        text = "原文" * 2000
        with self.assertRaises(PipelineError):
            _correct_chunk(fail, ["python", "cli.py", "correct", text], {}, text)
        self.assertEqual(len(calls), 4)
        def model_failure(args, **kwargs):
            return subprocess.CompletedProcess(args, 1, "", "missing model")
        with self.assertRaisesRegex(PipelineError, "missing model"):
            _correct_chunk(model_failure, ["python", "cli.py", "correct", text], {}, text)

    def test_truncated_correction_splits_and_preserves_text(self):
        chunk = "中文原文。" * 100
        calls = []
        def runner(args, **kwargs):
            text = args[args.index("correct") + 1]
            calls.append(text)
            if len(text) > 300:
                return subprocess.CompletedProcess(args, 1, "", "JSONDecodeError: truncated response")
            return subprocess.CompletedProcess(args, 0, json.dumps({"corrected": text, "applied_edits": []}), "")
        result = _correct_chunk(runner, ["python", "cli.py", "correct", chunk], {}, chunk)
        self.assertEqual(result["corrected"], chunk)
        self.assertGreater(len(calls), 1)

    def test_chunking_preserves_every_character(self):
        raw = "这是第一句。" * 800
        chunks = split_text(raw, max_chars=1500)
        self.assertEqual("".join(chunks), raw)
        self.assertTrue(all(len(chunk) <= 1500 for chunk in chunks))

    def test_python_subprocess_chinese_is_utf8(self):
        output = _invoke(subprocess.run, [sys.executable, "-c", "print('中文纠错')"])
        self.assertEqual(output.strip(), "中文纠错")

    def test_subprocess_failure_has_stage_detail(self):
        def fail(args, **kwargs):
            return subprocess.CompletedProcess(args, 7, "", "missing model")
        with self.assertRaisesRegex(PipelineError, "failed \\(7\\): missing model"):
            _invoke(fail, ["whisper", "transcribe"])


class PipelineTests(unittest.TestCase):
    def test_auto_retries_candidate_without_audio_and_keeps_raw(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "cat" / "cli").mkdir(parents=True)
            (root / "whisper").mkdir()
            (root / "llama").mkdir()
            raw_dir = root / "raw"
            raw_dir.mkdir()
            for suffix, value in [("txt", "五日三行五身。\n"), ("json", '{"segments": []}'), ("srt", "1\n")]:
                (raw_dir / f"raw.{suffix}").write_text(value, encoding="utf-8")
            calls = []

            def fake_run(args, **kwargs):
                from subprocess import CompletedProcess
                calls.append(args)
                if "sniff" in args:
                    captured = [dict(r, headers={":authority": "x.test", ":method": "GET", "range": "bytes=0-100", "referer": "https://x.test/watch", "user-agent": "browser"}) for r in RESOURCES]
                    return CompletedProcess(args, 0, json.dumps({"schemaVersion": 1, "resources": captured}), "")
                if "download" in args:
                    capture = json.loads(Path(args[args.index("--from") + 1]).read_text(encoding="utf-8"))
                    headers = capture["resources"][0]["headers"]
                    self.assertFalse(any(name.startswith(":") for name in headers))
                    self.assertNotIn("range", headers)
                    self.assertEqual(headers["referer"], "https://x.test/watch")
                    Path(args[args.index("-o") + 1]).write_bytes(b"media")
                    return CompletedProcess(args, 0, "", "")
                if "ffmpeg" in str(args[0]):
                    if "main.mp4" in str(kwargs.get("candidate_url")):
                        return CompletedProcess(args, 1, "", "no audio")
                    Path(args[-1]).write_bytes(b"audio")
                    return CompletedProcess(args, 0, "", "")
                if "transcribe" in args:
                    result = {"files": {ext: str(raw_dir / f"raw.{ext}") for ext in ("txt", "json", "srt")}}
                    return CompletedProcess(args, 0, json.dumps(result), "")
                if "correct" in args:
                    return CompletedProcess(args, 0, json.dumps({"corrected": "吾日三省吾身。\n", "applied_edits": []}), "")
                raise AssertionError(args)

            # The runner receives a candidate hint only for FFmpeg fixture behavior.
            def runner(args, **kwargs):
                if "ffmpeg" in str(args[0]):
                    downloads = [c for c in calls if "download" in c]
                    selected_id = downloads[-1][downloads[-1].index("--id") + 1]
                    kwargs["candidate_url"] = "main.mp4" if selected_id == "2" else "main.m4a"
                return fake_run(args, **kwargs)

            result = run_pipeline(
                "https://x.test/watch", output_root=root / "out", cat_dir=root / "cat",
                whisper_dir=root / "whisper", llama_dir=root / "llama", runner=runner,
                python_executable="python", node_executable="node", ffmpeg_executable="ffmpeg",
            )
            self.assertEqual(result["selected_resource"]["id"], 3)
            self.assertEqual((result["run_dir"] / "raw.txt").read_text(encoding="utf-8"), "五日三行五身。\n")
            self.assertEqual((result["run_dir"] / "corrected.txt").read_text(encoding="utf-8"), "吾日三省吾身。\n")


if __name__ == "__main__":
    unittest.main()
