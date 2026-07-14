from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
QUEUE_SCRIPT = PROJECT_ROOT / "scripts" / "retranscription_queue.ps1"
PWSH = shutil.which("pwsh")


@unittest.skipUnless(PWSH and QUEUE_SCRIPT.is_file(), "PowerShell queue is unavailable")
class PowerShellQueueTests(unittest.TestCase):
    def enqueue_command(
        self,
        *,
        queue_root: Path,
        media: Path,
        output: Path,
        metadata: Path,
        start: str,
        end: str,
    ) -> list[str]:
        return [
            str(PWSH), "-NoLogo", "-NoProfile", "-File", str(QUEUE_SCRIPT),
            "-Action", "enqueue",
            "-Media", str(media),
            "-Output", str(output),
            "-Metadata", str(metadata),
            "-Start", start,
            "-End", end,
            "-QueueRoot", str(queue_root),
        ]

    def test_concurrent_equivalent_ranges_create_one_atomic_job(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            media = root / "media.mp4"
            media.write_bytes(b"queue identity only")
            queue_root = root / "queue"
            commands = [
                self.enqueue_command(
                    queue_root=queue_root,
                    media=media,
                    output=root / "one.srt",
                    metadata=root / "one.json",
                    start="1.25",
                    end="2.5",
                ),
                self.enqueue_command(
                    queue_root=queue_root,
                    media=media,
                    output=root / "two.srt",
                    metadata=root / "two.json",
                    start="00:01.250",
                    end="00:02.500",
                ),
            ]
            processes = [
                subprocess.Popen(
                    command,
                    text=True,
                    encoding="utf-8",
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                for command in commands
            ]
            results = []
            for process in processes:
                stdout, stderr = process.communicate(timeout=20)
                self.assertEqual(process.returncode, 0, stderr)
                results.append(json.loads(stdout))

            self.assertEqual({item["job_id"] for item in results}, {results[0]["job_id"]})
            self.assertEqual(sorted(item["enqueued"] for item in results), [False, True])
            self.assertEqual(len(list((queue_root / "pending").glob("*.json"))), 1)
            self.assertEqual(len(list((queue_root / "identities").glob("*.json"))), 1)
            third = subprocess.run(
                self.enqueue_command(
                    queue_root=queue_root,
                    media=media,
                    output=root / "three.srt",
                    metadata=root / "three.json",
                    start="1.250",
                    end="2.500",
                ),
                check=True,
                text=True,
                encoding="utf-8",
                stdout=subprocess.PIPE,
                timeout=20,
            )
            duplicate = json.loads(third.stdout)
            self.assertTrue(duplicate["duplicate"])
            self.assertIn(
                Path(duplicate["existing_output"]).name,
                {"one.srt", "two.srt"},
            )

    def test_transcribe_cues_job_records_canonical_range(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            media = root / "media.mp4"
            media.write_bytes(b"queue identity only")
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({
                "source": {"video": str(media)},
                "cues": [
                    {"index": 1, "start_ms": 1000, "end_ms": 2000},
                    {"index": 2, "start_ms": 2200, "end_ms": 3000},
                ],
            }), encoding="utf-8")
            queue_root = root / "queue"
            command = [
                str(PWSH), "-NoLogo", "-NoProfile", "-File", str(QUEUE_SCRIPT),
                "-Action", "enqueue",
                "-JobType", "transcribe-cues",
                "-Manifest", str(manifest),
                "-Cues", "2,1",
                "-Padding", "0.5",
                "-Output", str(root / "out.srt"),
                "-Metadata", str(root / "out.json"),
                "-WordTimestamps",
                "-QueueRoot", str(queue_root),
            ]

            completed = subprocess.run(
                command,
                check=False,
                text=True,
                encoding="utf-8",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=20,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            job_path = next((queue_root / "pending").glob("*.json"))
            job = json.loads(job_path.read_text(encoding="utf-8-sig"))
            self.assertEqual(job["job_type"], "transcribe-cues")
            self.assertEqual(job["cues"], "1,2")
            self.assertEqual(job["identity"]["range_start_ms"], 500)
            self.assertEqual(job["identity"]["range_end_ms"], 3500)
            self.assertTrue(job["identity"]["word_timestamps"])

    def test_drain_invokes_transcribe_cues_through_single_worker(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            media = root / "media.mp4"
            media.write_bytes(b"queue command only")
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({
                "source": {"video": str(media)},
                "cues": [{"index": 1, "start_ms": 1000, "end_ms": 2000}],
            }), encoding="utf-8")
            queue_root = root / "queue"
            fake_log = root / "fake-log.json"
            fake_subflow = root / "fake_subflow.py"
            fake_subflow.write_text(
                "import json, os, sys\n"
                "open(os.environ['FAKE_SUBFLOW_LOG'], 'w', encoding='utf-8').write(json.dumps(sys.argv[1:]))\n",
                encoding="utf-8",
            )
            enqueue = [
                str(PWSH), "-NoLogo", "-NoProfile", "-File", str(QUEUE_SCRIPT),
                "-Action", "enqueue",
                "-JobType", "transcribe-cues",
                "-Manifest", str(manifest),
                "-Cues", "1",
                "-Padding", "0.5",
                "-Output", str(root / "out.srt"),
                "-Metadata", str(root / "out.json"),
                "-WordTimestamps",
                "-QueueRoot", str(queue_root),
            ]
            subprocess.run(enqueue, check=True, capture_output=True, text=True, timeout=20)
            drain = [
                str(PWSH), "-NoLogo", "-NoProfile", "-File", str(QUEUE_SCRIPT),
                "-Action", "drain",
                "-QueueRoot", str(queue_root),
                "-Python", sys.executable,
                "-Subflow", str(fake_subflow),
            ]
            environment = dict(os.environ)
            environment["FAKE_SUBFLOW_LOG"] = str(fake_log)

            completed = subprocess.run(
                drain,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=environment,
                timeout=20,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            arguments = json.loads(fake_log.read_text(encoding="utf-8"))
            self.assertEqual(arguments[0], "transcribe-cues")
            self.assertIn("--manifest", arguments)
            self.assertIn("--cues", arguments)
            self.assertIn("--word-timestamps", arguments)
            self.assertEqual(len(list((queue_root / "done").glob("*.json"))), 1)

    def test_drain_max_jobs_streams_one_completed_file_at_a_time(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            media = root / "media.mp4"
            media.write_bytes(b"queue command only")
            queue_root = root / "queue"
            fake_log = root / "fake-log.jsonl"
            fake_subflow = root / "fake_subflow.py"
            fake_subflow.write_text(
                "import json, os, sys\n"
                "with open(os.environ['FAKE_SUBFLOW_LOG'], 'a', encoding='utf-8') as stream:\n"
                "    stream.write(json.dumps(sys.argv[1:]) + '\\n')\n",
                encoding="utf-8",
            )
            for ordinal, (start, end) in enumerate(
                (("0", "1"), ("1", "2"), ("2", "3")),
                start=1,
            ):
                subprocess.run(
                    self.enqueue_command(
                        queue_root=queue_root,
                        media=media,
                        output=root / f"out-{ordinal}.srt",
                        metadata=root / f"out-{ordinal}.json",
                        start=start,
                        end=end,
                    ),
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=20,
                )

            drain = [
                str(PWSH), "-NoLogo", "-NoProfile", "-File", str(QUEUE_SCRIPT),
                "-Action", "drain", "-MaxJobs", "1",
                "-QueueRoot", str(queue_root),
                "-Python", sys.executable, "-Subflow", str(fake_subflow),
            ]
            environment = dict(os.environ)
            environment["FAKE_SUBFLOW_LOG"] = str(fake_log)

            first = subprocess.run(
                drain,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=environment,
                timeout=20,
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            first_status = json.loads(first.stdout)
            self.assertEqual(first_status["done"], 1)
            self.assertEqual(first_status["pending"], 2)
            self.assertEqual(len(first_status["processed_jobs"]), 1)
            self.assertEqual(
                Path(first_status["processed_jobs"][0]["output"]).name,
                "out-1.srt",
            )
            first_arguments = json.loads(fake_log.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(first_arguments[first_arguments.index("--start") + 1], "0")
            self.assertEqual(first_arguments[first_arguments.index("--end") + 1], "1")

            drain_all = [
                str(PWSH), "-NoLogo", "-NoProfile", "-File", str(QUEUE_SCRIPT),
                "-Action", "drain",
                "-QueueRoot", str(queue_root),
                "-Python", sys.executable, "-Subflow", str(fake_subflow),
            ]

            second = subprocess.run(
                drain_all,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=environment,
                timeout=20,
            )
            self.assertEqual(second.returncode, 0, second.stderr)
            second_status = json.loads(second.stdout)
            self.assertEqual(second_status["done"], 3)
            self.assertEqual(second_status["pending"], 0)
            self.assertEqual(len(second_status["processed_jobs"]), 2)
            self.assertEqual(
                [Path(item["output"]).name for item in second_status["processed_jobs"]],
                ["out-2.srt", "out-3.srt"],
            )
            self.assertEqual(len(fake_log.read_text(encoding="utf-8").splitlines()), 3)

    def test_changed_manifest_is_rejected_before_transcribe_cues_drain(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            media = root / "media.mp4"
            media.write_bytes(b"queue command only")
            manifest = root / "manifest.json"
            payload = {
                "manifest_id": "original",
                "source": {"video": str(media), "video_sha256": "abc"},
                "cues": [{"index": 1, "start_ms": 1000, "end_ms": 2000}],
            }
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            queue_root = root / "queue"
            fake_log = root / "fake-log.json"
            fake_subflow = root / "fake_subflow.py"
            fake_subflow.write_text(
                "import os\nopen(os.environ['FAKE_SUBFLOW_LOG'], 'w').write('called')\n",
                encoding="utf-8",
            )
            enqueue = [
                str(PWSH), "-NoLogo", "-NoProfile", "-File", str(QUEUE_SCRIPT),
                "-Action", "enqueue", "-JobType", "transcribe-cues",
                "-Manifest", str(manifest), "-Cues", "1",
                "-Output", str(root / "out.srt"),
                "-Metadata", str(root / "out.json"),
                "-QueueRoot", str(queue_root),
            ]
            subprocess.run(enqueue, check=True, capture_output=True, text=True, timeout=20)
            payload["cues"][0]["start_ms"] = 5000
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            environment = dict(os.environ)
            environment["FAKE_SUBFLOW_LOG"] = str(fake_log)
            drain = [
                str(PWSH), "-NoLogo", "-NoProfile", "-File", str(QUEUE_SCRIPT),
                "-Action", "drain", "-QueueRoot", str(queue_root),
                "-Python", sys.executable, "-Subflow", str(fake_subflow),
            ]

            completed = subprocess.run(
                drain,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=environment,
                timeout=20,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertFalse(fake_log.exists())
            self.assertEqual(len(list((queue_root / "failed").glob("*.json"))), 1)
            self.assertEqual(len(list((queue_root / "identities").glob("*.json"))), 0)

    def test_recover_removes_stale_unattached_identity_claim(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            queue_root = root / "queue"
            status = [
                str(PWSH), "-NoLogo", "-NoProfile", "-File", str(QUEUE_SCRIPT),
                "-Action", "status", "-QueueRoot", str(queue_root),
            ]
            subprocess.run(status, check=True, capture_output=True, text=True, timeout=20)
            claim = queue_root / "identities" / ("a" * 64 + ".json")
            claim.write_text('{"job_id":"stale"}', encoding="utf-8")
            old = claim.stat().st_mtime - 7200
            os.utime(claim, (old, old))
            recover = [
                str(PWSH), "-NoLogo", "-NoProfile", "-File", str(QUEUE_SCRIPT),
                "-Action", "recover", "-StaleAfterMinutes", "1",
                "-QueueRoot", str(queue_root),
            ]

            completed = subprocess.run(
                recover,
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=20,
            )
            report = json.loads(completed.stdout)
            self.assertEqual(report["removed_stale_reservations"], 1)
            self.assertFalse(claim.exists())


if __name__ == "__main__":
    unittest.main()
