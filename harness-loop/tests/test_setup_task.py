"""Tests for harness-loop/exam/setup_task.sh and finish_task.sh on local temp repos.

No Harbor and no network: bd, dolt and jq must already be on PATH (the scripts
only download them when missing), so every test skips cleanly without them.
"""
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

HERE = Path(__file__).resolve().parent
EXAM = HERE.parent / "exam"
SETUP = EXAM / "setup_task.sh"
FINISH = EXAM / "finish_task.sh"
INSTALLER = HERE.parent / "install_harness.py"
TOOLS = ("bd", "dolt", "jq", "git", "python3")
EXAM_AUTHOR = "Exam Bot <exam@example.invalid>"


@unittest.skipUnless(all(shutil.which(t) for t in TOOLS), "needs bd, dolt, jq on PATH")
class TaskRepoCase(unittest.TestCase):
    """Fixtures shared by the setup and finish tests: a packed harness, temp repos, logs."""

    @classmethod
    def setUpClass(cls):
        # The real agent-workflow harness at HEAD, packed the way WorkflowAgent packs it.
        cls.pack_dir = Path(tempfile.mkdtemp())
        target = cls.pack_dir / "harness"
        run([sys.executable, str(INSTALLER), "--commit", "HEAD", "--target", str(target)], HERE)
        cls.tarball = cls.pack_dir / "harness.tgz"
        run(["tar", "-czf", str(cls.tarball), "-C", str(target), ".claude", "AGENTS.md"], HERE)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.pack_dir, ignore_errors=True)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.origin = self.tmp / "origin.git"
        self.logs = self.tmp / "logs"
        self.instruction = self.tmp / "instruction.md"
        self.instruction.write_text("Fix the off-by-one in counter.py so count(3) returns 3.\n")
        self.repos = []

    def tearDown(self):
        for repo in self.repos:  # bd starts a dolt server per repo; never leak one
            if (repo / ".beads").exists():
                run(["bd", "dolt", "stop"], repo, check=False)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def setup_task(self, repo, test_command="python3 -m unittest", check=True):
        self.repos.append(Path(repo))
        cmd = ["bash", str(SETUP), str(repo), str(self.tarball), "Fix counter off-by-one",
               str(self.instruction), test_command, "exam-run-1", str(self.logs)]
        env = {"EXAM_ORIGIN": str(self.origin), "PATH": os.environ["PATH"],
               "HOME": os.environ["HOME"]}
        r = subprocess.run(cmd, cwd=self.tmp, env=env, text=True, capture_output=True)
        if check:
            self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def task_repo(self, files=None):
        path, _ = make_repo(self.tmp / "app", files or {"counter.py": "def count(n):\n    return n - 1\n"})
        return path

    def git(self, repo, *args):
        return run(["git", *args], repo).stdout.strip()

    def bead_id(self, result):
        return result.stdout.strip().splitlines()[-1]


class SetupTaskTest(TaskRepoCase):

    def test_origin_is_local_bare_repo_and_main_fetches(self):
        """
        /work pushes to and branches off `origin`; in the sandbox there is no
        GitHub, so origin must be a local bare repo with main on it. If it is
        missing, every worktree add off origin/main and every push fails.
        """
        # GIVEN a task repo
        repo = self.task_repo()
        # WHEN setup runs
        self.setup_task(repo)
        # THEN origin is the file:// bare repo and `git fetch origin main` works
        self.assertEqual(self.git(repo, "remote", "get-url", "origin"), f"file://{self.origin}")
        self.assertEqual(self.git(self.origin, "rev-parse", "--is-bare-repository"), "true")
        run(["git", "fetch", "origin", "main"], repo)
        self.assertEqual(self.git(repo, "rev-parse", "origin/main"), self.git(repo, "rev-parse", "HEAD"))

    def test_harness_is_committed_and_tree_is_clean(self):
        """
        The harness must be committed as `exam: harness` and EXAM_MODE excluded,
        so the tree is clean. If the harness were only on disk, worktrees off
        origin/main would have no skills; if EXAM_MODE were tracked, every
        worktree would think it is the exam's main root.
        """
        # GIVEN a task repo
        repo = self.task_repo()
        # WHEN setup runs
        self.setup_task(repo)
        # THEN the harness version file is committed, EXAM_MODE exists but is ignored
        self.assertEqual(self.git(repo, "log", "-1", "--format=%s"), "exam: harness")
        self.assertIn(".claude/HARNESS_VERSION", self.git(repo, "ls-files", ".claude"))
        self.assertEqual((repo / ".claude/EXAM_MODE").read_text().strip(), "exam-run-1")
        self.assertNotIn(".claude/EXAM_MODE", self.git(repo, "ls-files", ".claude"))
        self.assertEqual(self.git(repo, "status", "--porcelain"), "")

    def test_worktree_off_origin_main_has_skills_and_gates_row(self):
        """
        The coordinator makes each bead's worktree off origin/main and its
        implementer reads quality.md, the coordinator skill and the Quality
        Gates row inside that worktree. This is the exact failure a reviewer
        found in the plan: a git-excluded harness is absent from worktrees.
        """
        # GIVEN a set-up task repo
        repo = self.task_repo()
        self.setup_task(repo, test_command="python3 -m unittest -v")
        # WHEN a worktree is added off origin/main, as the coordinator does
        wt = self.tmp / "app-bd-x"
        run(["git", "worktree", "add", "-q", str(wt), "origin/main"], repo)
        # THEN it holds the skills and the repo's gates row
        self.assertTrue((wt / ".claude/skills/standards/quality.md").is_file())
        self.assertTrue((wt / ".claude/skills/coordinator/SKILL.md").is_file())
        self.assertIn("| repo | `python3 -m unittest -v` | — |", (wt / "CLAUDE.md").read_text())
        self.assertFalse((wt / ".claude/EXAM_MODE").exists())

    def test_existing_claude_md_is_kept_and_gates_appended(self):
        """
        A repo's own CLAUDE.md is task context the agent should still read;
        setup appends the Quality Gates section instead of replacing it.
        """
        # GIVEN a repo that tracks its own CLAUDE.md
        repo = self.task_repo({"counter.py": "x = 1\n", "CLAUDE.md": "# Repo rules\nUse tabs.\n"})
        # WHEN setup runs
        self.setup_task(repo)
        # THEN the original text comes first and the gates section follows
        text = (repo / "CLAUDE.md").read_text()
        self.assertTrue(text.startswith("# Repo rules\nUse tabs.\n"))
        self.assertIn("## Quality Gates", text)
        self.assertIn("| repo | `python3 -m unittest` | — |", text)

    def test_tracked_claude_dir_and_agents_md_move_aside(self):
        """
        A repo that already tracks `.claude/` and `AGENTS.md` would collide with
        the harness. They move to `.claude-repo/` and `AGENTS.repo.md` (history
        kept via git mv) and CLAUDE.md points at them, so neither the harness
        nor the repo's own notes are lost.
        """
        # GIVEN a repo tracking .claude/ and AGENTS.md
        repo = self.task_repo({"counter.py": "x = 1\n", ".claude/settings.json": "{\"repo\": 1}\n",
                               "AGENTS.md": "repo agents\n"})
        # WHEN setup runs
        self.setup_task(repo)
        # THEN the repo's files sit under the new names and the harness owns the old ones
        self.assertEqual((repo / ".claude-repo/settings.json").read_text(), "{\"repo\": 1}\n")
        self.assertEqual((repo / "AGENTS.repo.md").read_text(), "repo agents\n")
        self.assertTrue((repo / ".claude/skills/coordinator/SKILL.md").is_file())
        self.assertNotEqual((repo / "AGENTS.md").read_text(), "repo agents\n")
        claude = (repo / "CLAUDE.md").read_text()
        self.assertIn("AGENTS.repo.md", claude)
        self.assertIn(".claude-repo/", claude)

    def test_claude_md_symlink_to_agents_md_keeps_its_text(self):
        """
        Many repos make CLAUDE.md a symlink to AGENTS.md. Moving AGENTS.md
        would leave a dangling link and lose the repo's notes, so setup keeps
        the text as a real file before appending the gates.
        """
        # GIVEN CLAUDE.md -> AGENTS.md
        repo = self.task_repo({"counter.py": "x = 1\n", "AGENTS.md": "shared notes\n"})
        (repo / "CLAUDE.md").symlink_to("AGENTS.md")
        run(GIT + ["add", "-A"], repo)
        run(GIT + ["commit", "-qm", "link"], repo)
        # WHEN setup runs
        self.setup_task(repo)
        # THEN CLAUDE.md is a regular file starting with the old text
        self.assertFalse((repo / "CLAUDE.md").is_symlink())
        self.assertTrue((repo / "CLAUDE.md").read_text().startswith("shared notes\n"))

    def test_one_bead_exists_and_bd_show_works_from_a_worktree(self):
        """
        /work is started on the printed bead id; the coordinator and its
        subagents run `bd show` from sibling worktrees. In the .4 toy run a
        subagent's `bd show` once found no database, so check it from a
        worktree too.
        """
        # GIVEN a set-up task repo
        repo = self.task_repo()
        bead = self.bead_id(self.setup_task(repo))
        # WHEN bd is asked from the repo and from a worktree off origin/main
        count = json.loads(run(["bd", "count", "--json"], repo).stdout)["count"]
        wt = self.tmp / "app-bd-y"
        run(["git", "worktree", "add", "-q", str(wt), "origin/main"], repo)
        shown = json.loads(run(["bd", "show", bead, "--json"], wt).stdout)[0]
        # THEN exactly one bead exists, with the task's title and instruction text
        self.assertEqual(count, 1)
        self.assertEqual(shown["id"], bead)
        self.assertEqual(shown["title"], "Fix counter off-by-one")
        self.assertIn("count(3) returns 3", shown["description"])

    def test_bd_init_leaves_no_trace_in_the_repo(self):
        """
        `bd init` writes AGENTS.md and .gitignore into the repo root. Those
        would land in the harness commit (AGENTS.md overwritten, .gitignore
        changed) and pollute the graded tree, so setup reverts them.
        """
        # GIVEN a repo tracking its own .gitignore
        repo = self.task_repo({"counter.py": "x = 1\n", ".gitignore": "build/\n"})
        # WHEN setup runs
        self.setup_task(repo)
        # THEN .gitignore is untouched and .beads/ is untracked
        self.assertEqual((repo / ".gitignore").read_text(), "build/\n")
        self.assertEqual(self.git(repo, "ls-files", ".beads"), "")

    def test_rerun_is_idempotent(self):
        """
        Harbor may retry setup; a second run must reuse the commits, origin
        and bead instead of failing or creating a second bead.
        """
        # GIVEN a set-up task repo
        repo = self.task_repo()
        first = self.bead_id(self.setup_task(repo))
        head = self.git(repo, "rev-parse", "HEAD")
        # WHEN setup runs again
        second = self.bead_id(self.setup_task(repo))
        # THEN nothing new was made
        self.assertEqual(first, second)
        self.assertEqual(self.git(repo, "rev-parse", "HEAD"), head)
        self.assertEqual(json.loads(run(["bd", "count", "--json"], repo).stdout)["count"], 1)

    def test_detached_head_becomes_main(self):
        """
        Monthly-SWEBench images check the base commit out detached and drop
        origin. The coordinator branches off origin/main, so setup must put
        the task state on a branch named main.
        """
        # GIVEN a repo on a detached HEAD with an uncommitted edit
        repo = self.task_repo()
        run(["git", "checkout", "-q", "--detach"], repo)
        (repo / "counter.py").write_text("def count(n):\n    return n - 2\n")
        # WHEN setup runs
        self.setup_task(repo)
        # THEN main exists, holds the edit in `exam: base`, and origin/main matches
        self.assertEqual(self.git(repo, "rev-parse", "--abbrev-ref", "HEAD"), "main")
        base = (self.logs / "base.sha").read_text().strip()
        self.assertEqual(self.git(repo, "log", "-1", "--format=%s", base), "exam: base")
        self.assertIn("n - 2", self.git(repo, "show", f"{base}:counter.py"))

    def test_plain_directory_is_initialised_without_dependency_dirs(self):
        """
        Terminal-Bench images have /app with no git repo at all. Setup must
        make one, and must not commit node_modules: tens of thousands of
        files would blow the setup time limit.
        """
        # GIVEN a plain directory with code and a node_modules dir
        app = self.tmp / "plain"
        (app / "node_modules/x").mkdir(parents=True)
        (app / "node_modules/x/index.js").write_text("1\n")
        (app / "main.py").write_text("print(1)\n")
        # WHEN setup runs
        self.setup_task(app)
        # THEN main.py is committed in the base and node_modules is not
        tracked = self.git(app, "ls-files")
        self.assertIn("main.py", tracked)
        self.assertNotIn("node_modules", tracked)
        self.assertEqual(self.git(app, "status", "--porcelain"), "")

    def test_no_visible_test_command_says_so(self):
        """
        Many tasks have no visible tests (manifest test_command null). The
        gates row must say so plainly rather than hold an empty command an
        implementer would try to run.
        """
        # GIVEN a task with no visible test command
        repo = self.task_repo()
        # WHEN setup runs with an empty command
        self.setup_task(repo, test_command="")
        # THEN the row says there are none
        self.assertIn("| repo | none (no visible tests) | — |", (repo / "CLAUDE.md").read_text())

    def test_pipe_in_test_command_does_not_break_the_table(self):
        """
        A `|` inside the command would end the markdown cell early and the
        implementer would read a truncated command.
        """
        # GIVEN a command containing a pipe
        repo = self.task_repo()
        # WHEN setup runs
        self.setup_task(repo, test_command="make test | tee out.txt")
        # THEN the pipe is escaped
        self.assertIn("`make test \\| tee out.txt`", (repo / "CLAUDE.md").read_text())

    def test_logs_and_identity(self):
        """
        base.sha / start.sha / bead.txt feed the try-folder export, and the
        exam commits must use a fixed neutral identity, never a real email.
        """
        # GIVEN a task repo
        repo = self.task_repo()
        # WHEN setup runs
        bead = self.bead_id(self.setup_task(repo))
        # THEN the logs name the base, harness commit and bead, authored by the exam bot
        start = (self.logs / "start.sha").read_text().strip()
        self.assertEqual(start, self.git(repo, "rev-parse", "HEAD"))
        self.assertEqual((self.logs / "bead.txt").read_text().strip(), bead)
        self.assertTrue((self.logs / "base.sha").is_file())
        self.assertEqual(self.git(repo, "log", "-1", "--format=%an <%ae>"), EXAM_AUTHOR)

    def test_setup_is_fast_with_tools_present(self):
        """
        Harbor gives setup 360 s. With the tools already installed the repo
        work itself must take a small fraction of that, leaving the budget
        for downloads in the sandbox.
        """
        # GIVEN a task repo
        repo = self.task_repo()
        # WHEN setup runs
        t0 = time.monotonic()
        self.setup_task(repo)
        # THEN it took well under a minute
        self.assertLess(time.monotonic() - t0, 60)

    def test_bad_arguments_exit_2(self):
        """
        A missing argument must fail loudly before touching the repo, not
        half-set-up a task.
        """
        # GIVEN only two arguments
        # WHEN setup runs
        r = subprocess.run(["bash", str(SETUP), str(self.tmp), "x"], text=True, capture_output=True)
        # THEN it exits 2 with usage
        self.assertEqual(r.returncode, 2)
        self.assertIn("usage", r.stderr)


class FinishTaskTest(TaskRepoCase):
    """Post-run checkout: the verifier grades the workdir, /work built on a branch."""

    def push_bead_branch(self, repo, bead, change):
        wt = self.tmp / "wt"
        run(["git", "worktree", "add", "-q", "-b", f"feature/bd-{bead}-fix-counter", str(wt), "origin/main"], repo)
        (wt / "counter.py").write_text(change)
        run(["git", "commit", "-qam", "fix"], wt)
        run(["git", "push", "-q", "origin", "HEAD"], wt)

    def finish(self, repo):
        return run(["bash", str(FINISH), str(repo), str(self.logs)], self.tmp)

    def test_checks_out_the_pushed_bead_branch(self):
        """
        /work builds in a worktree and pushes the bead branch; the workdir the
        verifier grades is still at base. Without this checkout every task
        would score 0 no matter what /work built.
        """
        # GIVEN a bead branch with a fix pushed to origin
        repo = self.task_repo()
        bead = self.bead_id(self.setup_task(repo))
        self.push_bead_branch(repo, bead, "def count(n):\n    return n\n")
        # WHEN the finish step runs
        self.finish(repo)
        # THEN the workdir holds the fix and the export files describe it
        self.assertEqual((repo / "counter.py").read_text(), "def count(n):\n    return n\n")
        end = (self.logs / "end.sha").read_text().strip()
        self.assertEqual(end, self.git(repo, "rev-parse", "HEAD"))
        self.assertIn("+    return n\n", (self.logs / "diff.patch").read_text())
        self.assertEqual((self.logs / "branch.txt").read_text().strip(), f"feature/bd-{bead}-fix-counter")

    def test_without_a_bead_branch_stays_at_base(self):
        """
        If /work never pushed a branch the workdir stays at the harness commit
        (it will score 0) and the export says end == start with an empty diff.
        """
        # GIVEN a set-up repo with nothing pushed
        repo = self.task_repo()
        self.setup_task(repo)
        start = (self.logs / "start.sha").read_text().strip()
        # WHEN the finish step runs
        self.finish(repo)
        # THEN nothing moved
        self.assertEqual(self.git(repo, "rev-parse", "HEAD"), start)
        self.assertEqual((self.logs / "end.sha").read_text().strip(), start)
        self.assertEqual((self.logs / "diff.patch").read_text(), "")
        self.assertEqual((self.logs / "branch.txt").read_text().strip(), "")

    def test_branch_of_a_longer_bead_id_is_not_taken(self):
        """
        Bead `exam-1` must not pick up `feature/bd-exam-12-...`: a substring
        match would grade another bead's work.
        """
        # GIVEN only a branch for a different bead whose id starts with ours
        repo = self.task_repo()
        bead = self.bead_id(self.setup_task(repo))
        run(["git", "push", "-q", "origin", f"HEAD:refs/heads/feature/bd-{bead}9-other"], repo)
        run(["git", "push", "-q", "origin", f"HEAD:refs/heads/scratch"], repo)
        # WHEN the finish step runs
        self.finish(repo)
        # THEN no branch is chosen
        self.assertEqual((self.logs / "branch.txt").read_text().strip(), "")

    def test_lone_branch_without_the_bead_id_is_taken(self):
        """
        If the coordinator named its branch some other way, the only non-main
        branch on the exam's private origin can only be this bead's work.
        """
        # GIVEN one non-main branch whose name lacks the bead id
        repo = self.task_repo()
        self.setup_task(repo)
        run(["git", "push", "-q", "origin", "HEAD:refs/heads/fix-counter"], repo)
        # WHEN the finish step runs
        self.finish(repo)
        # THEN it is chosen
        self.assertEqual((self.logs / "branch.txt").read_text().strip(), "fix-counter")


if __name__ == "__main__":
    unittest.main()
