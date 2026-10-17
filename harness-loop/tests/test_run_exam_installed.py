"""Tests for run_exam.py --installed DIR (the canary's harness under test: an already-installed .claude +
AGENTS.md tree) and its HARNESS_EXAM_ENV_FILE token, with test_run_exam's fake `harbor run` and `docker`.

The base class is reached through its module (test_run_exam.RunExamTest) so discovery does not run its tests twice.
"""
import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import test_run_exam as base
from helpers import GIT, installed_tree, run

sys.path.insert(0, str(base.EXAM.parent))
import install_harness  # noqa: E402

DUMMY = "dummy-plan-value-1ik4"
VERSION = {"commit": "c" * 40, "addon": None, "addon_sha256": None}


class InstalledRun(base.RunExamTest):
    """run_exam.py --installed, on RunExamTest's fixtures (its own tests are dropped below)."""

    def tree(self, version=VERSION):
        return installed_tree(self.root / "install", version)

    def test_installed_tree_reaches_harbor_and_its_hash_labels_the_run(self):
        """
        What: --installed packs the given tree (WorkflowAgent harness_installed=<abs dir>), and run.json plus each
        result record installed_sha256 (install_harness's recipe) and the tree's HARNESS_VERSION commit.
        Why: the canary of an installing repo tests its installed files, which no agent-workflow sha names.
        Breaks: a harness_commit kwarg (installs agent-workflow instead) or no installed_sha256 (no baseline reuse).
        """
        # GIVEN one task and an installed tree pinned to commit ccc...
        m = self.manifest({"t1": 2}, screen=("t1",))
        tree = self.tree()
        # WHEN run_exam runs it with --installed
        self.run_exam("--tasks", m, "--budget-usd", 100, "--screen", harness=("--installed", tree))
        # THEN harbor got harness_installed=<abs tree> and no harness_commit
        (argv,) = self.argvs()
        self.assertIn(f"harness_installed={tree.resolve()}", argv)
        self.assertFalse(any(a.startswith("harness_commit=") for a in argv))
        # AND run.json and the try carry the tree's hash and pinned commit, and the try counts
        run = self.only_run()
        meta = json.loads((run / "run.json").read_text())
        want = install_harness.installed_sha256(tree)
        self.assertEqual((meta["installed_sha256"], meta["harness_commit"], meta["harness_version"]),
                         (want, VERSION["commit"], VERSION))
        r = self.results(run)["t1"]
        self.assertEqual((r["status"], r["installed_sha256"]), ("ok", want))
        self.assertIn(want[:7], run.name)

    def test_try_that_ran_another_installed_tree_is_an_error(self):
        """
        What: a try whose result.json names another installed_sha256 is status error.
        Why: the commit check cannot see an installed tree; the hash is its only label.
        Breaks: a sandbox that packed the wrong tree would be scored as the candidate.
        """
        # GIVEN a fake harbor that reports a different tree hash
        m = self.manifest({"t1": 2})
        self.spec.write_text(json.dumps({"t1": {"installed_sha256": "0" * 64}}))
        # WHEN run_exam runs --installed
        self.run_exam("--tasks", m, "--budget-usd", 100, harness=("--installed", self.tree()), check=False)
        # THEN the try is an error
        self.assertEqual(self.results(self.only_run())["t1"]["status"], "error")

    def test_tree_without_harness_version_records_null_commit(self):
        """
        What: a tree with no .claude/HARNESS_VERSION (an agent-workflow checkout) runs with harness_commit null.
        Why: the source tree's no-PR canary passes such trees. Breaks: a crash, or a made-up commit.
        """
        # GIVEN a tree without HARNESS_VERSION
        m = self.manifest({"t1": 2})
        tree = installed_tree(self.root / "plain")
        # WHEN run_exam runs it
        self.run_exam("--tasks", m, "--budget-usd", 100, harness=("--installed", tree))
        # THEN commit and version are null, the hash is set, the try counts
        meta = json.loads((self.only_run() / "run.json").read_text())
        self.assertEqual((meta["harness_commit"], meta["harness_version"]), (None, None))
        self.assertEqual(meta["installed_sha256"], install_harness.installed_sha256(tree))
        self.assertEqual(self.results(self.only_run())["t1"]["status"], "ok")

    def test_installed_tree_with_an_addon_in_the_default_home_exits_2(self):
        """
        What: a tree whose HARNESS_VERSION names an add-on refuses the default ~/.harness-loop, exit 2.
        Why: the improver reads that home's runs/ and writes public PRs; an installing repo's tree is private.
        Breaks: private skills land where a public PR can quote them.
        """
        # GIVEN HARNESS_LOOP_HOME = the user's default home and a tree with an add-on
        m = self.manifest({"t1": 2})
        user = self.root / "user"
        user.mkdir()
        (user / ".harness-loop").symlink_to(self.home)
        tree = self.tree({**VERSION, "addon": ".claude/harness-addon", "addon_sha256": "a" * 64})
        # WHEN run_exam runs it
        p = self.run_exam("--tasks", m, "--budget-usd", 100, harness=("--installed", tree), check=False,
                          extra_env={"HOME": str(user)})
        # THEN exit 2 naming HARNESS_LOOP_HOME, and Harbor never ran
        self.assertEqual(p.returncode, 2, p.stderr)
        self.assertIn("HARNESS_LOOP_HOME", p.stderr)
        self.assertFalse(self.log.exists())

    def test_installed_with_harness_or_addon_is_a_usage_error(self):
        """
        What: --installed with --harness, or with --addon, exits 2 before any run.
        Why: the two name different harnesses; picking one silently would label a run with the wrong one.
        """
        m = self.manifest({"t1": 2})
        addon, _ = self.addon_dir()
        for extra in (("--harness", "HEAD"), ("--addon", addon)):
            with self.subTest(extra=extra[0]):
                # WHEN run_exam gets both
                p = self.run_exam("--tasks", m, "--budget-usd", 100, *extra, check=False,
                                  harness=("--installed", self.tree()))
                # THEN exit 2, no harbor call
                self.assertEqual(p.returncode, 2, p.stderr)
                self.assertFalse(self.log.exists())

    def test_env_file_token_reaches_harbor_only(self):
        """
        What: with no CLAUDE_CODE_OAUTH_TOKEN, run_exam takes it from HARNESS_EXAM_ENV_FILE and hands it to Harbor.
        Why: a laptop shell need not export the plan token. Breaks: the exam refuses, or the value is logged.
        """
        # GIVEN no token in the env and a scratch env file holding a dummy one
        m = self.manifest({"t1": 2})
        env_file = self.root / "exam.env"
        env_file.write_text(f"OTHER=1\nexport CLAUDE_CODE_OAUTH_TOKEN='{DUMMY}'\n")
        # WHEN run_exam runs
        p = self.run_exam("--tasks", m, "--budget-usd", 100, harness=("--installed", self.tree()),
                          extra_env={"CLAUDE_CODE_OAUTH_TOKEN": "", "HARNESS_EXAM_ENV_FILE": str(env_file)})
        # THEN harbor got exactly the file's token (and no OTHER), and the value is in no output or run file
        (env,) = self.envs()
        self.assertNotIn("OTHER", env)
        self.assertEqual(self.token_hashes(), [hashlib.sha256(DUMMY.encode()).hexdigest()])
        for text in (p.stdout, p.stderr, *(f.read_text(errors="replace") for f in self.home.rglob("*")
                                          if f.is_file())):
            self.assertNotIn(DUMMY, text)

    def test_missing_env_file_is_refused_naming_the_path(self):
        """
        What: HARNESS_EXAM_ENV_FILE naming no file exits before any run, naming the path.
        Why: a typo must not start an exam that then fails every try.
        Breaks: every try errors and is booked at its cap.
        """
        # GIVEN HARNESS_EXAM_ENV_FILE naming a file that does not exist
        m = self.manifest({"t1": 2})
        missing = self.root / "nope.env"
        # WHEN run_exam runs
        p = self.run_exam("--tasks", m, "--budget-usd", 100, harness=("--installed", self.tree()), check=False,
                          extra_env={"CLAUDE_CODE_OAUTH_TOKEN": "", "HARNESS_EXAM_ENV_FILE": str(missing)})
        # THEN it exits non-zero naming the path, and Harbor never ran
        self.assertNotEqual(p.returncode, 0)
        self.assertIn(str(missing), p.stderr)
        self.assertFalse(self.log.exists())

    def test_tree_holding_local_or_secret_files_is_refused_naming_the_path(self):
        """
        What: an installed tree (not in git) holding .claude/.env, .claude/.env.local, a *.bak or
        .claude/settings.local.json exits 2 naming that file's path, never its contents, before Harbor runs.
        Why: installed_files packs every file of a plain tree into the sandbox, which runs the harness under test.
        Breaks: a working checkout's secrets copied into Docker.
        """
        m = self.manifest({"t1": 2})
        for rel in (".claude/.env", ".claude/.env.local", ".claude/skills/x/SKILL.md.bak",
                    ".claude/settings.local.json"):
            with self.subTest(rel=rel):
                # GIVEN a plain installed tree holding that file with a dummy secret
                tree = installed_tree(self.root / f"t-{abs(hash(rel))}", VERSION)
                (tree / rel).parent.mkdir(parents=True, exist_ok=True)
                (tree / rel).write_text(f"CLAUDE_CODE_OAUTH_TOKEN={DUMMY}\n")
                # WHEN run_exam runs it
                p = self.run_exam("--tasks", m, "--budget-usd", 100, harness=("--installed", tree), check=False)
                # THEN exit 2 naming the path, not the value, and Harbor never ran
                self.assertEqual(p.returncode, 2, p.stderr)
                self.assertIn(str((tree / rel).resolve()), p.stderr)
                self.assertNotIn(DUMMY, p.stdout + p.stderr)
                self.assertFalse(self.log.exists())


class InstalledFiles(unittest.TestCase):
    """install_harness.installed_files: what an --installed tree packs into the sandbox (and hashes)."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def git_tree(self):
        """A git work tree whose installed files are committed; returns its root."""
        tree = installed_tree(self.root / "checkout", VERSION)
        run(GIT + ["init", "-q"], tree)
        run(GIT + ["add", "-A"], tree)
        run(GIT + ["commit", "-qm", "install"], tree)
        return tree

    def test_git_work_tree_leaves_out_ignored_files(self):
        """
        What: in a git work tree, installed_files leaves out git-ignored files: an ignored .claude/.env,
        .claude/settings.local.json and .claude/worktrees/ file are not listed.
        Why: `--candidate ../app-pr` is a developer's working checkout, full of local secrets and worktrees, which
        its .gitignore keeps out of git. Breaks: the sandbox (and installed_sha256) gets the .env and every worktree.
        """
        # GIVEN a committed install that ignores its local files, plus those files holding a dummy secret
        tree = self.git_tree()
        (tree / ".gitignore").write_text(".claude/.env\n.claude/settings.local.json\n.claude/worktrees/\n")
        for rel in (".claude/.env", ".claude/settings.local.json", ".claude/worktrees/w/AGENTS.md"):
            (tree / rel).parent.mkdir(parents=True, exist_ok=True)
            (tree / rel).write_text(f"CLAUDE_CODE_OAUTH_TOKEN={DUMMY}\n")
        # WHEN its installed files are listed
        rels = [rel for rel, _ in install_harness.installed_files(tree)]
        # THEN only the committed ones
        self.assertEqual(rels, [".claude/HARNESS_VERSION", ".claude/skills/implementer/SKILL.md", "AGENTS.md"])

    def test_untracked_trees_inside_a_git_repo_are_packed_and_hash_apart(self):
        """
        What: two installed trees sitting untracked (not ignored) inside a git work tree each list their own files,
        and differing trees get different installed_sha256 values.
        Why: canary trees are often unpacked under a checkout without being committed; `git ls-files` alone lists
        nothing there. Breaks: every such tree hashes as empty, so loop.py posts "not needed (installed trees
        identical)" for two trees that differ, and the exam never runs.
        """
        # GIVEN a git repo with one commit, holding two untracked trees that differ in one skill line
        repo = self.root / "repo"
        repo.mkdir()
        run(GIT + ["init", "-q"], repo)
        (repo / "README.md").write_text("repo\n")
        run(GIT + ["add", "-A"], repo)
        run(GIT + ["commit", "-qm", "init"], repo)
        a = installed_tree(repo / "a", VERSION)
        b = installed_tree(repo / "b", VERSION, skill="Builds one bead, carefully.\n")
        # WHEN their installed files and hashes are taken
        rels = [rel for rel, _ in install_harness.installed_files(a)]
        # THEN each tree's own files are listed and the hashes differ
        self.assertEqual(rels, [".claude/HARNESS_VERSION", ".claude/skills/implementer/SKILL.md", "AGENTS.md"])
        self.assertNotEqual(install_harness.installed_sha256(a), install_harness.installed_sha256(b))

    def test_untracked_dotenv_not_ignored_is_refused(self):
        """
        What: an untracked .claude/.env that no .gitignore covers raises ValueError naming its path, not its value.
        Why: untracked files are now packed; a secret must still never reach the sandbox.
        Breaks: a checkout without a .gitignore entry ships its .env into Docker.
        """
        # GIVEN a committed install plus an untracked, unignored dummy .claude/.env
        tree = self.git_tree()
        (tree / ".claude/.env").write_text(f"CLAUDE_CODE_OAUTH_TOKEN={DUMMY}\n")
        # WHEN its installed files are listed
        with self.assertRaises(ValueError) as cm:
            install_harness.installed_files(tree)
        # THEN the error names the path only
        self.assertIn(str(tree / ".claude/.env"), str(cm.exception))
        self.assertNotIn(DUMMY, str(cm.exception))

    def test_tree_with_no_harness_file_is_refused(self):
        """
        What: a git work tree whose .claude is wholly ignored, and a plain tree with an empty .claude, raise
        ValueError "no harness files under <root>".
        Why: an empty listing hashes the same for every such tree. Breaks: two unrelated trees compare
        "identical" and the canary passes without testing anything.
        """
        # GIVEN a git repo that ignores .claude/, and a plain tree with an empty .claude
        ignored = self.git_tree()
        (ignored / ".gitignore").write_text(".claude/\n")
        run(GIT + ["rm", "-rq", "--cached", ".claude"], ignored)
        empty = self.root / "empty"
        (empty / ".claude").mkdir(parents=True)
        for tree in (ignored, empty):
            with self.subTest(tree=tree.name):
                # WHEN its installed files are listed
                with self.assertRaises(ValueError) as cm:
                    install_harness.installed_files(tree)
                # THEN the refusal names the root
                self.assertIn(f"no harness files under {tree}", str(cm.exception))

    def test_git_work_tree_with_a_tracked_dotenv_is_refused(self):
        """
        What: a tracked .claude/.env in a git work tree raises ValueError naming its path, not its value.
        Why: a committed secret is still a secret; the sandbox runs the harness under test.
        Breaks: a repo that committed its .env ships it into Docker.
        """
        # GIVEN a git work tree with a committed dummy .claude/.env
        tree = self.git_tree()
        (tree / ".claude/.env").write_text(f"CLAUDE_CODE_OAUTH_TOKEN={DUMMY}\n")
        run(GIT + ["add", "-A"], tree)
        run(GIT + ["commit", "-qm", "oops"], tree)
        # WHEN its installed files are listed
        with self.assertRaises(ValueError) as cm:
            install_harness.installed_files(tree)
        # THEN the error names the path only
        self.assertIn(str(tree / ".claude/.env"), str(cm.exception))
        self.assertNotIn(DUMMY, str(cm.exception))

    def test_plain_tree_with_a_dotenv_is_refused(self):
        """
        What: a plain (non-git) tree holding .claude/.env raises ValueError naming its path, not its value.
        Why: a plain tree packs every file. Breaks: secrets into the sandbox.
        """
        # GIVEN a plain installed tree with a dummy .claude/.env
        tree = installed_tree(self.root / "plain", VERSION)
        (tree / ".claude/.env").write_text(f"CLAUDE_CODE_OAUTH_TOKEN={DUMMY}\n")
        # WHEN its installed files are listed
        with self.assertRaises(ValueError) as cm:
            install_harness.installed_files(tree)
        # THEN the error names the path only
        self.assertIn(str(tree / ".claude/.env"), str(cm.exception))
        self.assertNotIn(DUMMY, str(cm.exception))


# Drop the inherited tests from this class (they run in test_run_exam).
for _name in [n for n in vars(base.RunExamTest) if n.startswith("test_")]:
    setattr(InstalledRun, _name, None)


if __name__ == "__main__":
    unittest.main()
