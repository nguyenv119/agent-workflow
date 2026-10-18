"""Tests for harness-loop/tasks/fetch_cache.py, on a copy of the script in a throwaway harness-loop/ layout.

The "Hugging Face" source is a local file:// tree (HARNESS_TASKS_URL) holding plain tar archives named
<archive>.tar.zst (`tar -xf` detects the format), and `uv` is a fake on PATH that logs its argv and makes
.venv/bin/{python,harbor}. Nothing touches the network.
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

TASKS = Path(__file__).resolve().parents[1] / "tasks"
REV = "47393eb5b1e73711c33fb6c6da39f3a1197d932e"
FAKE_UV = """#!/bin/sh
echo "$*" >> "$FAKE_UV_LOG"
if [ "$1" = venv ]; then mkdir -p "$2/bin" && touch "$2/bin/python"; fi
if [ "$1" = pip ]; then touch "$(dirname "$4")/harbor"; fi
"""


def task_files(tid):
    return {f"{tid}/task.toml": "[environment]\ncpus = 2\n", f"{tid}/tests/test.sh": "true\n",
            f"{tid}/instruction.md": "Fix it.\n"}


class FetchCache(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp(prefix="fetch-cache-test-")))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.loop = self.tmp / "harness-loop"
        (self.loop / "tasks").mkdir(parents=True)
        (self.loop / "exam").mkdir()
        (self.loop / "exam/requirements.txt").write_text("harbor\n")
        for f in ("fetch_cache.py", "manifest.py"):
            shutil.copy(TASKS / f, self.loop / "tasks" / f)
        self.src = self.tmp / "hf"
        self.archives = self.src / f"datasets/Org/Set-2026/resolve/{REV}"
        self.archives.mkdir(parents=True)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        (self.bin / "uv").write_text(FAKE_UV)
        (self.bin / "uv").chmod(0o755)
        self.uv_log = self.tmp / "uv.log"

    def archive(self, name, ids):
        with tarfile.open(self.archives / f"{name}.tar.zst", "w") as tar:
            for tid in ids:
                for rel, text in task_files(tid).items():
                    data = text.encode()
                    info = tarfile.TarInfo(rel)
                    info.size = len(data)
                    tar.addfile(info, io.BytesIO(data))

    def manifest(self, rows):
        tasks = [{"id": tid, "harbor_path": f"cache/set-2026/{arch}/{tid}", "repo": None, "pr": None,
                  "merged_at": None, "language": "go", "kind": "bugfix", "workdir": "/app", "test_command": None,
                  "title": tid, "screen": True} for arch, tid in rows]
        m = self.loop / "tasks/m.json"
        m.write_text(json.dumps({"set": "m", "source": f"huggingface:Org/Set-2026@{REV} (hard)", "tasks": tasks}))
        return m

    def fetch(self, *args, env=None):
        return subprocess.run([sys.executable, str(self.loop / "tasks/fetch_cache.py"), *args], cwd=self.tmp,
                              env={**os.environ, "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
                                   "HARNESS_TASKS_URL": self.src.as_uri(), "FAKE_UV_LOG": str(self.uv_log),
                                   **(env or {})},
                              capture_output=True, text=True)

    def test_fetches_only_the_manifest_tasks_then_checks_and_builds_the_venv(self):
        """
        What: a fresh layout gets exactly the manifest's folders from each archive, passes manifest.py
        --require-cache, and gets .venv from requirements.txt via uv.
        Why: a fresh clone has no git-ignored cache and no venv, so the canary could not start.
        Breaks: other tasks unpacked (wasted disk), or a missing venv found only when Harbor is called.
        """
        # GIVEN two archives holding three tasks, and a manifest naming one task from each
        self.archive("bugfix", ["a1", "a2"])
        self.archive("non_bugfix", ["b1"])
        self.manifest([("bugfix", "a1"), ("non_bugfix", "b1")])
        # WHEN fetched by a path relative to harness-loop/
        p = self.fetch("tasks/m.json")
        # THEN exit 0, the two folders exist, the third does not, and the check and venv ran
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        cache = self.loop / "tasks/cache/set-2026"
        self.assertTrue((cache / "bugfix/a1/tests/test.sh").is_file())
        self.assertTrue((cache / "non_bugfix/b1/task.toml").is_file())
        self.assertFalse((cache / "bugfix/a2").exists())
        self.assertIn("leak-checked 2/2", p.stdout)
        self.assertTrue((self.loop / ".venv/bin/harbor").is_file())
        log = self.uv_log.read_text()
        self.assertIn(f"venv {self.loop / '.venv'} --python 3.12", log)
        self.assertIn(f"-r {self.loop / 'exam/requirements.txt'}", log)
        # AND a second run downloads and installs nothing
        shutil.rmtree(self.archives)
        self.uv_log.unlink()
        p = self.fetch("tasks/m.json")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertFalse(self.uv_log.exists())

    def test_harness_tasks_cache_is_linked_not_downloaded(self):
        """
        What: HARNESS_TASKS_CACHE holding the folders makes symlinks and downloads nothing.
        Why: a laptop already has the cache; re-downloading ~60 MB per canary is waste.
        """
        # GIVEN an existing cache with a1 and no archive at the source
        old = self.tmp / "old-cache"
        for rel, text in task_files("a1").items():
            (old / "set-2026/bugfix" / rel).parent.mkdir(parents=True, exist_ok=True)
            (old / "set-2026/bugfix" / rel).write_text(text)
        self.manifest([("bugfix", "a1")])
        # WHEN fetched
        p = self.fetch("tasks/m.json", env={"HARNESS_TASKS_CACHE": str(old)})
        # THEN the folder is a link into the old cache
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        dest = self.loop / "tasks/cache/set-2026/bugfix/a1"
        self.assertTrue(dest.is_symlink())
        self.assertEqual(dest.resolve(), (old / "set-2026/bugfix/a1").resolve())

    def test_missing_archive_exits_1_naming_the_url(self):
        """What: a source without the archive exits 1 naming the URL tried; nothing is half-written."""
        self.manifest([("bugfix", "a1")])
        p = self.fetch("tasks/m.json")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn(f"resolve/{REV}/bugfix.tar.zst", p.stderr)
        self.assertFalse((self.loop / "tasks/cache/set-2026/bugfix/a1").exists())

    def test_task_missing_from_the_archive_exits_1(self):
        """What: an archive without the manifest's id exits 1 (tar names the missing member)."""
        self.archive("bugfix", ["a2"])
        self.manifest([("bugfix", "a1")])
        p = self.fetch("tasks/m.json")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("unpacking bugfix.tar.zst failed", p.stderr)

    def test_manifest_without_a_huggingface_source_is_exit_2(self):
        """What: a source with no pinned huggingface part is a usage error: there is nothing to fetch from."""
        m = self.manifest([("bugfix", "a1")])
        m.write_text(m.read_text().replace("huggingface:", "github:"))
        p = self.fetch(str(m))
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)

    def test_canary_manifest_is_valid_and_fetchable(self):
        """
        What: tasks/canary-2026-10.json passes manifest.py and its paths fit fetch_cache's layout.
        Why: the canary names it by path; a typo would surface only in the real acceptance.
        """
        sys.path.insert(0, str(TASKS))
        import fetch_cache
        import manifest
        data = manifest.load(TASKS / "canary-2026-10.json")
        self.assertEqual((data["set"], len(data["tasks"])), ("canary-2026-10", 3))
        self.assertTrue(all(t["screen"] for t in data["tasks"]))
        (_, name, _), tasks = fetch_cache.wanted(TASKS / "canary-2026-10.json")
        self.assertEqual(name, "Monthly-SWEBench-2026-05")
        self.assertEqual(sorted(a for _, _, a, _ in tasks), ["bugfix", "bugfix", "non_bugfix"])


if __name__ == "__main__":
    unittest.main()
