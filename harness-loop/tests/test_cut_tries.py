"""Tests for .claude/skills/coordinator/cut_tries.py, on fake try folders with faked dates."""
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / ".claude/skills/coordinator/cut_tries.py"
DAY = 86400


class CutTriesTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.now = time.time()

    def tearDown(self):
        shutil.rmtree(self.root)

    def make_try(self, bead, n, result, age_days, size=10, pin=False, repo="r"):
        """Create a try folder dated age_days ago (note.md mtime) plus its index row."""
        folder = self.root / repo / bead / f"try-{n}"
        folder.mkdir(parents=True)
        for f in ("start.sha", "end.sha", "diff.patch", "gates.txt"):
            (folder / f).write_text(f + "\n")
        (folder / "transcript.jsonl").write_bytes(b"x" * size)
        (folder / "note.md").write_text("note\n")
        if pin:
            (folder / "PIN").write_text("")
        t = self.now - age_days * DAY
        os.utime(folder / "note.md", (t, t))
        with open(self.root / repo / "index.tsv", "a") as fh:
            fh.write(f"2026-01-01T00:00:00Z\t{bead}\t{n}\t{result}\t1\t1\t{bead}/try-{n}\n")
        return folder

    def cut(self, *args):
        env = {**os.environ, "HARNESS_TRIES_DIR": str(self.root)}
        return subprocess.run([sys.executable, str(SCRIPT), *args], env=env,
                              text=True, capture_output=True, check=True)

    def test_age_rules_delete_only_old_transcripts(self):
        """
        D7's retention: a passed try's transcript goes after 90 days, a failed
        try's after 365, because failures are what the improver learns from.
        Swapping the rules, or deleting by folder mtime that a deletion itself
        refreshes, would throw away the records most worth keeping.
        """
        # GIVEN pass tries aged 100 and 30 days, fail tries aged 100 and 400 days
        old_pass = self.make_try("b.1", 1, "pass", 100)
        new_pass = self.make_try("b.2", 1, "pass", 30)
        mid_fail = self.make_try("b.3", 1, "fail", 100)
        old_fail = self.make_try("b.4", 1, "fail", 400)
        # WHEN cut_tries runs
        self.cut()
        # THEN only the 100-day pass and 400-day fail transcripts are gone
        self.assertFalse((old_pass / "transcript.jsonl").exists())
        self.assertFalse((old_fail / "transcript.jsonl").exists())
        self.assertTrue((new_pass / "transcript.jsonl").exists())
        self.assertTrue((mid_fail / "transcript.jsonl").exists())
        # AND notes, diffs, shas and gates stay in every folder
        for d in (old_pass, old_fail):
            for f in ("note.md", "diff.patch", "start.sha", "end.sha", "gates.txt"):
                self.assertTrue((d / f).exists(), f"{d.name}/{f}")
        # AND each deletion is logged with its reason
        log = (self.root / "cut.log").read_text().splitlines()
        self.assertEqual(len(log), 2)
        self.assertTrue(any("b.1/try-1" in l and "age" in l for l in log))
        # AND index rows are untouched
        self.assertEqual(len((self.root / "r/index.tsv").read_text().splitlines()), 4)

    def test_pin_file_keeps_transcript(self):
        """
        A PIN file marks a try someone wants kept (e.g. an exam control).
        If PIN were ignored, a 400-day-old pinned failure would be cut.
        """
        # GIVEN a 400-day-old failed try with a PIN file
        pinned = self.make_try("b.1", 1, "fail", 400, pin=True)
        # WHEN cut_tries runs
        self.cut()
        # THEN its transcript stays and nothing is logged
        self.assertTrue((pinned / "transcript.jsonl").exists())
        self.assertFalse((self.root / "cut.log").exists())

    def test_size_cap_deletes_oldest_unpinned_until_under(self):
        """
        Past the size cap, transcripts go oldest first until the root fits,
        skipping pinned ones. If the cap deleted newest first, or deleted
        everything, recent tries the next attempt needs would vanish.
        """
        # GIVEN four young tries of 1000 bytes each; the oldest is pinned
        pinned = self.make_try("b.1", 1, "fail", 40, size=1000, pin=True)
        older = self.make_try("b.2", 1, "fail", 30, size=1000)
        middle = self.make_try("b.3", 1, "fail", 20, size=1000)
        newest = self.make_try("b.4", 1, "fail", 10, size=1000)
        # WHEN cut_tries runs with a cap that fits about two transcripts
        self.cut("--cap-bytes", "2600")
        # THEN the two oldest unpinned go, the newest and the pinned stay
        self.assertFalse((older / "transcript.jsonl").exists())
        self.assertFalse((middle / "transcript.jsonl").exists())
        self.assertTrue((newest / "transcript.jsonl").exists())
        self.assertTrue((pinned / "transcript.jsonl").exists())
        self.assertIn("cap", (self.root / "cut.log").read_text())

    def test_dry_run_deletes_nothing(self):
        """
        --dry-run lists what would go so a person can check before deleting.
        If it deleted anyway, or wrote cut.log, the preview would be a lie.
        """
        # GIVEN a 400-day-old failed try
        old = self.make_try("b.1", 1, "fail", 400)
        # WHEN cut_tries runs with --dry-run
        out = self.cut("--dry-run").stdout
        # THEN it names the transcript, keeps it, and writes no log
        self.assertIn("b.1/try-1/transcript.jsonl", out)
        self.assertTrue((old / "transcript.jsonl").exists())
        self.assertFalse((self.root / "cut.log").exists())


if __name__ == "__main__":
    unittest.main()
