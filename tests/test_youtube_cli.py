from __future__ import annotations

import argparse
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import subflow


class YoutubeCliTests(unittest.TestCase):
    def test_default_runtime_is_project_local(self) -> None:
        runtime = subflow.youtube_runtime_python()
        self.assertIn(subflow.PROJECT_ROOT, runtime.parents)
        self.assertIn(".runtime", runtime.parts)

    def test_parser_defaults_to_single_mp4_video(self) -> None:
        args = subflow.build_parser().parse_args([
            "download-youtube", "https://youtu.be/abc123",
        ])
        self.assertFalse(args.playlist)
        self.assertFalse(args.audio_only)
        self.assertEqual(args.container, "mp4")
        self.assertEqual(args.format, "bv*+ba/b")

    def test_rejects_non_youtube_or_insecure_urls(self) -> None:
        for url in ("http://youtube.com/watch?v=x", "https://example.com/video"):
            with self.subTest(url=url), self.assertRaises(subflow.WorkflowError):
                subflow.validate_youtube_url(url)

    def test_download_command_uses_no_playlist_and_ffmpeg(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            python = root / "python.exe"
            python.write_bytes(b"")
            ffmpeg = root / "ffmpeg.exe"
            ffprobe = root / "ffprobe.exe"
            ffmpeg.write_bytes(b"")
            ffprobe.write_bytes(b"")
            args = argparse.Namespace(
                url="https://www.youtube.com/watch?v=abc123&list=playlist",
                runtime_root=root,
                runtime_python=python,
                output_dir=root / "downloads",
                ffmpeg_root=None,
                output_template="%(title)s.%(ext)s",
                format="bv*+ba/b",
                container="mp4",
                audio_only=False,
                audio_format="mp3",
                playlist=False,
                force=False,
            )
            completed = mock.Mock(stdout=str(root / "downloads" / "video.mp4") + "\n")
            with mock.patch("subflow.tool_paths", return_value=(ffmpeg, ffprobe)), mock.patch(
                "subflow.run", return_value=completed,
            ) as run_mock:
                self.assertEqual(subflow.command_download_youtube(args), 0)
            command = run_mock.call_args.args[0]
            self.assertIn("--no-playlist", command)
            self.assertIn("--no-write-subs", command)
            self.assertIn("--no-write-auto-subs", command)
            self.assertEqual(command[command.index("--ffmpeg-location") + 1], str(root))
            self.assertEqual(command[-1], args.url)


if __name__ == "__main__":
    unittest.main()
