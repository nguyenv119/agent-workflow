"""Tests for .claude/skills/coordinator/save_try.py, run against real temp git repos."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from helpers import GIT, make_repo, run

SCRIPT = Path(__file__).resolve().parents[2] / ".claude/skills/coordinator/save_try.py"
FILES = ["start.sha", "end.sha", "diff.patch", "gates.txt", "transcript.jsonl", "note.md"]
BEAD = "demo-abc.3"


def write_transcript(path, first_message, mtime=None):
    """Write a minimal subagent transcript whose first user message is first_message."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        {"type": "user", "message": {"role": "user", "content": first_message}},
        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}},
    ]
    path.write_text("".join(json.dumps(x) + "\n" for x in lines))
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


class SaveTryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo, self.start = make_repo(self.tmp / "myrepo", {"a.py": "x = 1\n", "b.bin": "keep\n"})
        _, self.mid = make_repo(self.repo, {"a.py": "x = 2\ny = 3\n", "c.py": "new\n"})
        self.tries = self.tmp / "tries"
        self.config = self.tmp / "config"
        (self.tmp / "gates1.txt").write_text("FAIL: test_x (AssertionError)\n")
        (self.tmp / "note1.md").write_text("Try 1 changed a.py; test_x fails.\n")
        self.env = {**os.environ, "HARNESS_TRIES_DIR": str(self.tries),
                    "CLAUDE_CONFIG_DIR": str(self.config)}

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def save(self, *args, check=True):
        cmd = [sys.executable, str(SCRIPT), "--bead", BEAD, *args]
        return subprocess.run(cmd, cwd=self.tmp, env=self.env, text=True,
                              capture_output=True, check=check)

    def save_worktree(self, start, result="fail", *extra, check=True):
        return self.save("--worktree", str(self.repo), "--start", start,
                         "--gates", str(self.tmp / "gates1.txt"), "--result", result,
                         "--note", str(self.tmp / "note1.md"), *extra, check=check)

    def index_rows(self):
        return [l.split("\t") for l in (self.tries / "myrepo/index.tsv").read_text().splitlines()]

    def test_two_tries_make_two_folders_and_two_index_rows(self):
        """
        Each implementer attempt must get its own numbered folder and one index
        row, in order. If the second save overwrote try-1 or skipped the index,
        the next attempt and the improver would lose the failed try's record,
        which is the whole point of keeping full records.
        """
        # GIVEN a start commit and an implementer commit, and a gates file
        # WHEN save_try runs for a failed try, then (after one more commit) a passed try
        out1 = self.save_worktree(self.start, "fail").stdout.strip()
        _, end2 = make_repo(self.repo, {"a.py": "x = 3\n"})
        out2 = self.save_worktree(self.mid, "pass").stdout.strip()
        # THEN try-1 and try-2 exist with all six files, each non-empty
        base = self.tries / "myrepo" / BEAD
        self.assertEqual(out1, str(base / "try-1"))
        self.assertEqual(out2, str(base / "try-2"))
        for d in ("try-1", "try-2"):
            for f in FILES[:4] + ["note.md"]:
                self.assertGreater((base / d / f).stat().st_size, 0, f"{d}/{f}")
        # AND the shas are the ones each try started and ended on
        self.assertEqual((base / "try-1/start.sha").read_text().strip(), self.start)
        self.assertEqual((base / "try-1/end.sha").read_text().strip(), self.mid)
        self.assertEqual((base / "try-2/end.sha").read_text().strip(), end2)
        # AND index.tsv has two rows: fail then pass, with relative folder paths
        rows = self.index_rows()
        self.assertEqual([r[1:4] for r in rows], [[BEAD, "1", "fail"], [BEAD, "2", "pass"]])
        self.assertEqual([r[6] for r in rows], [f"{BEAD}/try-1", f"{BEAD}/try-2"])

    def test_diff_patch_applied_to_start_reproduces_end_tree(self):
        """
        diff.patch must be the exact change from start to end, so a reader can
        replay the try. If it were taken against the wrong base (e.g. the
        working tree, or end..start), applying it to start would not give
        end's tree and the record would mislead.
        """
        # GIVEN a saved try from start to mid
        folder = Path(self.save_worktree(self.start).stdout.strip())
        check = self.tmp / "check"
        run(GIT + ["clone", "-q", str(self.repo), str(check)], self.tmp)
        run(GIT + ["checkout", "-q", self.start], check)
        # WHEN its diff.patch is applied to a checkout of start
        run(GIT + ["apply", "--check", str(folder / "diff.patch")], check)
        run(GIT + ["apply", "--index", str(folder / "diff.patch")], check)
        # THEN the resulting tree hash equals end's tree hash
        tree = run(GIT + ["write-tree"], check).stdout.strip()
        self.assertEqual(tree, run(GIT + ["rev-parse", self.mid + "^{tree}"], self.repo).stdout.strip())

    def test_index_counts_files_and_lines_changed(self):
        """
        The index's files_changed and lines_changed let the improver scan many
        tries without opening each diff. a.py loses 1 line and gains 2, c.py
        gains 1: 2 files, 4 lines. A miscount would make big tries look small.
        """
        # GIVEN start -> mid changes a.py (-1 +2) and adds c.py (+1)
        # WHEN the try is saved
        self.save_worktree(self.start)
        # THEN the row records 2 files and 4 changed lines
        self.assertEqual(self.index_rows()[0][4:6], ["2", "4"])

    def test_from_files_writes_identical_folder(self):
        """
        The exam (bead B13) has no worktree and hands over precomputed files.
        --from-files must write the same folder layout and bytes as worktree
        mode, or exam tries and live tries would not be readable by the same
        improver.
        """
        # GIVEN a folder saved in worktree mode, with a transcript
        tx = write_transcript(self.tmp / "t.jsonl", f"implementer for bead {BEAD}")
        live = Path(self.save_worktree(self.start, "fail", "--transcript", str(tx)).stdout.strip())
        src = self.tmp / "src"
        src.mkdir()
        for f in FILES:
            shutil.copy(live / f, src / f)
        # WHEN the same files are saved via --from-files under another repo name
        exam = Path(self.save("--from-files", str(src), "--repo-name", "exam", "--result", "fail").stdout.strip())
        # THEN the folder holds the same file names with the same bytes
        self.assertEqual(sorted(os.listdir(exam)), sorted(os.listdir(live)))
        for f in FILES:
            self.assertEqual((exam / f).read_bytes(), (live / f).read_bytes(), f)
        # AND the index row matches apart from the date
        exam_row = (self.tries / "exam/index.tsv").read_text().rstrip("\n").split("\t")
        self.assertEqual(exam_row[1:], self.index_rows()[0][1:])

    def test_finds_transcript_by_bead_id_and_skips_lookalikes(self):
        """
        Without an agent id, the transcript is found by searching subagent
        files for the bead id. Real prompts mention other beads ("such as
        demo-abc.2"), ids share prefixes (demo-abc.30), and old sessions linger.
        The search must pick the file whose first message is about this bead,
        made after the start commit, or the record holds someone else's work.
        """
        # GIVEN three subagent transcripts after the start commit, plus one before it
        proj = self.config / "projects/-repo/sess1/subagents"
        now = time.time()
        right = write_transcript(proj / "agent-right.jsonl", f"You are the implementer for bead {BEAD}. Go.", now + 10)
        write_transcript(proj / "agent-other.jsonl", f"implementer for bead demo-abc.4; see {BEAD} for context", now + 20)
        write_transcript(proj / "agent-prefix.jsonl", "implementer for bead demo-abc.30", now + 30)
        write_transcript(proj / "agent-old.jsonl", f"implementer for bead {BEAD}", now - 10**8)
        # WHEN save_try runs with no --transcript and no --agent-id
        folder = Path(self.save_worktree(self.start).stdout.strip())
        # THEN the copied transcript is byte-identical to the right one
        self.assertEqual((folder / "transcript.jsonl").read_bytes(), right.read_bytes())

    def test_agent_id_picks_that_agents_transcript(self):
        """
        The coordinator sees the implementer's agentId in the Agent tool
        result, which names the transcript file exactly. --agent-id must use
        it even when a newer transcript also names the bead, since the
        id is certain and the search is a guess.
        """
        # GIVEN two transcripts naming the bead; agent-aaa is older
        proj = self.config / "projects/-repo/sess1/subagents"
        aaa = write_transcript(proj / "agent-aaa.jsonl", f"implementer for bead {BEAD} try 1")
        write_transcript(proj / "agent-bbb.jsonl", f"implementer for bead {BEAD} try 2")
        # WHEN save_try runs with --agent-id aaa
        folder = Path(self.save_worktree(self.start, "fail", "--agent-id", "aaa").stdout.strip())
        # THEN agent-aaa's transcript is the one copied
        self.assertEqual((folder / "transcript.jsonl").read_bytes(), aaa.read_bytes())

    def test_no_transcript_writes_missing_file_with_the_search(self):
        """
        When no transcript matches, the folder still needs a record of why,
        so a reader can tell "never searched" from "searched and found none".
        transcript.missing must name the pattern searched and the bead id.
        """
        # GIVEN no subagent transcripts at all
        # WHEN save_try runs
        folder = Path(self.save_worktree(self.start).stdout.strip())
        # THEN transcript.missing exists, names the search, and there is no transcript.jsonl
        text = (folder / "transcript.missing").read_text()
        self.assertIn("subagents/agent-*.jsonl", text)
        self.assertIn(BEAD, text)
        self.assertFalse((folder / "transcript.jsonl").exists())

    def test_bad_explicit_transcript_fails_without_half_folder(self):
        """
        An explicit --transcript that does not exist is a caller bug. It must
        fail loudly and leave no try folder or index row behind; a half-written
        try-1 would shift every later try number and look like real data.
        """
        # GIVEN a --transcript path that does not exist
        # WHEN save_try runs
        res = self.save_worktree(self.start, "fail", "--transcript", str(self.tmp / "nope.jsonl"), check=False)
        # THEN it exits non-zero and writes nothing under the tries root
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("nope.jsonl", res.stderr)
        self.assertFalse((self.tries / "myrepo").exists())

    def test_rejects_result_other_than_pass_or_fail(self):
        """
        result feeds cut_tries' retention rule (pass 90 days, fail 365). Any
        other value would silently get the wrong rule, so it is refused.
        """
        # GIVEN --result ok
        # WHEN save_try runs
        res = self.save_worktree(self.start, "ok", check=False)
        # THEN it exits non-zero and writes nothing
        self.assertNotEqual(res.returncode, 0)
        self.assertFalse(self.tries.exists())

    def test_repo_name_is_main_repo_basename_from_a_worktree(self):
        """
        Tries from every worktree of one repo belong under one repo folder.
        Using the worktree's own directory name would scatter one bead's
        tries across folders named after branches.
        """
        # GIVEN a linked worktree of myrepo, in a dir named differently
        wt = self.tmp / "myrepo-bd-x"
        run(GIT + ["worktree", "add", "-q", str(wt), "-b", "feat"], self.repo)
        # WHEN save_try runs against the worktree
        folder = Path(self.save("--worktree", str(wt), "--start", self.start,
                                "--gates", str(self.tmp / "gates1.txt"), "--result", "fail",
                                "--note", str(self.tmp / "note1.md")).stdout.strip())
        # THEN the folder lives under the main repo's name
        self.assertEqual(folder, self.tries / "myrepo" / BEAD / "try-1")


if __name__ == "__main__":
    unittest.main()
