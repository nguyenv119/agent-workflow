"""Tests for harness-loop/tasks/build_from_pr.py, run against a synthetic git repo."""
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import unittest
from pathlib import Path

from helpers import make_repo, run, tree_hashes

TASKS = Path(__file__).resolve().parents[1] / "tasks"
SCRIPT = TASKS / "build_from_pr.py"
sys.path.insert(0, str(TASKS))
import manifest  # noqa: E402

BIN_OLD = bytes(range(256)) * 4
BIN_NEW = b"\x00\xff" * 300
BASE = {
    "src/app.js": "v1\n",
    "src/old.js": "to be deleted\n",
    "tests/app.test.js": "old test\n",
    "tests/untouched.test.js": "same\n",
    "docs/private/notes.md": "private notes\n",
    "README.md": "readme\n",
}
MERGE = {
    "src/app.js": "v2\n",
    "src/old.js": None,
    "src/new.js": "new file\n",
    "tests/app.test.js": "new test\n",
    "tests/new.test.js": "brand new test\n",
    "docs/private/notes.md": "private notes v2\n",
}
TEST_FILES = ("tests/app.test.js", "tests/new.test.js")


class BuildFromPrTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo, self.base = make_repo(self.tmp / "repo", BASE)
        (self.repo / "src/logo.bin").write_bytes(BIN_OLD)
        _, self.base = make_repo(self.repo, {})
        (self.repo / "src/logo.bin").write_bytes(BIN_NEW)
        _, self.merge = make_repo(self.repo, MERGE)
        self.instruction = self.tmp / "instruction.md"
        self.instruction.write_text("Make the app say v2.\n")
        self.out = self.tmp / "task"

    def tearDown(self):
        run(["rm", "-rf", str(self.tmp)], "/")

    def build(self, *extra, merge=None, test_files=TEST_FILES, command="true", check=True):
        cmd = [sys.executable, str(SCRIPT), "--repo", str(self.repo), "--merge", merge or self.merge,
               "--instruction", str(self.instruction), "--test-command", command,
               "--out", str(self.out), "--id", "demo-task", "--title", "Say v2"]
        for f in test_files:
            cmd += ["--test-file", f]
        p = subprocess.run(cmd + list(extra), text=True, capture_output=True)
        if check:
            self.assertEqual(p.returncode, 0, p.stderr)
        return p

    def env_tar(self):
        with tarfile.open(self.out / "environment/repo.tar.gz") as tar:
            return {m.name: tar.extractfile(m).read() for m in tar.getmembers() if m.isfile()}

    def extract_env(self, dest):
        dest.mkdir(parents=True)
        with tarfile.open(self.out / "environment/repo.tar.gz") as tar:
            tar.extractall(dest, filter="data")
        return dest

    def run_test_sh(self, workdir, verifier):
        env = {**os.environ, "TESTS_DIR": str(self.out / "tests"), "VERIFIER_DIR": str(verifier),
               "REPO_DIR": str(workdir)}
        subprocess.run(["bash", str(self.out / "tests/test.sh")], env=env, capture_output=True)
        return (verifier / "reward.txt").read_text().strip()

    def test_env_tarball_is_base_tree_minus_tests_and_excluded_paths(self):
        """
        The agent's starting repo must be the PR's parent commit with the PR's
        own test files and excluded paths removed, and nothing else changed.
        If a hidden test leaked in, the agent could read the grader; if another
        file differed, the task would not be the real pre-PR state.
        """
        # GIVEN a merged PR touching two test files, and a private dir to exclude
        # WHEN the task is built
        self.build("--exclude", "docs/private")
        # THEN the tarball has neither test file nor the excluded dir
        files = self.env_tar()
        for path in (*TEST_FILES, "docs/private/notes.md"):
            self.assertNotIn(path, files)
        # AND every other merge^ file is byte-identical
        expected = {p: c.encode() for p, c in BASE.items()
                    if p not in TEST_FILES and not p.startswith("docs/private")}
        expected["src/logo.bin"] = BIN_OLD
        self.assertEqual(files, expected)

    def test_dotenv_in_tree_is_refused_and_nothing_written(self):
        """
        A tracked .env would ship secrets into a rented sandbox. The builder
        must refuse, name the path, and write nothing, so a half-built task
        folder never exists to be uploaded by mistake.
        """
        for n, env_path in enumerate(("config/.env.local", "sub/.env")):
            with self.subTest(env_path=env_path):
                # GIVEN a repo whose base commit tracks that file
                repo, _ = make_repo(self.tmp / f"repo-env{n}", {env_path: "TOKEN=x\n", "a.js": "1\n",
                                                               "a.test.js": "t1\n"})
                self.repo, self.merge = make_repo(repo, {"a.js": "2\n", "a.test.js": "t2\n"})
                # WHEN the task is built
                p = self.build(test_files=("a.test.js",), check=False)
                # THEN it fails naming the path and the fix, and wrote nothing
                self.assertNotEqual(p.returncode, 0)
                self.assertIn(env_path, p.stderr)
                self.assertIn("--exclude", p.stderr)
                self.assertFalse(self.out.exists())

    def test_dotenv_is_accepted_when_excluded(self):
        """Excluding the .env path is the documented way past the secret guard."""
        # GIVEN a repo whose base commit tracks a .env
        repo, _ = make_repo(self.tmp / "repo2", {".env": "TOKEN=x\n", "a.js": "1\n", "a.test.js": "t1\n"})
        self.repo, self.merge = make_repo(repo, {"a.js": "2\n", "a.test.js": "t2\n"})
        # WHEN built with --exclude .env
        self.build("--exclude", ".env", test_files=("a.test.js",))
        # THEN the tarball holds no .env
        self.assertEqual(set(self.env_tar()), {"a.js"})

    def test_fix_patch_on_plain_base_dir_reproduces_merge_non_test_tree(self):
        """
        The oracle applies fix.patch to the unpacked tarball, which is not a git
        repo. It must reproduce the merge commit's non-test tree exactly,
        binary edits and deletions included, or the oracle control fails and
        the task cannot be trusted.
        """
        # GIVEN a built task
        self.build("--exclude", "docs/private")
        work = self.extract_env(self.tmp / "work")
        self.assertNotIn(b"docs/private", (self.out / "solution/fix.patch").read_bytes())
        # WHEN fix.patch is applied with git apply --binary in that plain dir
        run(["git", "apply", "--binary", str(self.out / "solution/fix.patch")], work)
        # THEN the tree equals merge's tree minus tests and the excluded dir
        ref = self.tmp / "ref"
        ref.mkdir()
        archive = subprocess.run(["git", "-C", str(self.repo), "archive", self.merge],
                                 capture_output=True, check=True).stdout
        tarfile.open(fileobj=io.BytesIO(archive)).extractall(ref, filter="data")
        want = {p: h for p, h in tree_hashes(ref).items()
                if p not in TEST_FILES and not p.startswith("docs/private")}
        self.assertEqual(tree_hashes(work), want)
        self.assertFalse((work / "src/old.js").exists())

    def test_solve_sh_applies_the_patch_in_a_plain_workdir(self):
        """solve.sh is what Harbor's oracle runs; it must work outside a git repo."""
        # GIVEN a built task and its unpacked tarball as the workdir
        self.build()
        work = self.extract_env(self.tmp / "work")
        # WHEN solve.sh runs with the solution dir and workdir overridden
        env = {**os.environ, "SOLUTION_DIR": str(self.out / "solution"), "REPO_DIR": str(work)}
        subprocess.run(["bash", str(self.out / "solution/solve.sh")], env=env, check=True,
                       capture_output=True)
        # THEN the fix landed
        self.assertEqual((work / "src/app.js").read_text(), "v2\n")

    def test_tests_files_hold_merge_versions(self):
        """The hidden tests are the PR's tests as merged, not the pre-PR versions."""
        # GIVEN / WHEN a built task
        self.build()
        # THEN tests/files holds each test file's content at merge
        self.assertEqual((self.out / "tests/files/tests/app.test.js").read_text(), "new test\n")
        self.assertEqual((self.out / "tests/files/tests/new.test.js").read_text(), "brand new test\n")

    def test_test_file_absent_at_merge_is_refused(self):
        """A test file that does not exist at merge cannot be a hidden test."""
        # GIVEN / WHEN a build naming a missing test file
        p = self.build(test_files=("tests/nope.test.js",), check=False)
        # THEN it fails naming it and writes nothing
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("tests/nope.test.js", p.stderr)
        self.assertFalse(self.out.exists())

    def test_test_file_unchanged_by_pr_is_refused(self):
        """
        A test the PR did not touch says nothing about the PR's change, and the
        nop control would likely pass it; refuse it.
        """
        # GIVEN / WHEN a build naming a test file the PR left alone
        p = self.build(test_files=("tests/untouched.test.js",), check=False)
        # THEN it fails naming it
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("tests/untouched.test.js", p.stderr)
        self.assertFalse(self.out.exists())

    def test_test_file_that_is_a_directory_is_refused(self):
        """A --test-file must be one file at merge; a directory is not a hidden test file."""
        # GIVEN / WHEN a build naming the tests/ directory
        p = self.build(test_files=("tests",), check=False)
        # THEN it fails naming it and writes nothing
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("not a file", p.stderr)
        self.assertFalse(self.out.exists())

    def test_bracketed_paths_are_literal_not_globs(self):
        """
        Next.js routes look like app/[id]/page.test.tsx. As a git pathspec
        `[id]` is a glob matching app/i/ and app/d/, so excluding it must not
        also drop app/i/page.test.tsx from fix.patch.
        """
        # GIVEN a PR changing app/[id]/page.test.tsx and app/i/page.test.tsx
        _, self.merge = make_repo(self.repo, {"app/[id]/page.test.tsx": "t\n", "app/i/page.test.tsx": "x\n"})
        # WHEN built with the bracketed path as the test file
        self.build(test_files=("app/[id]/page.test.tsx",))
        # THEN fix.patch holds app/i/page.test.tsx and not the test file
        patch = (self.out / "solution/fix.patch").read_text()
        self.assertIn("app/i/page.test.tsx", patch)
        self.assertNotIn("app/[id]/page.test.tsx", patch)

    def test_task_toml_network_with_allowlist(self):
        """
        With the opt-in flags the verifier has no network (hidden tests run
        offline, nothing can phone out) and the agent may reach exactly the
        allowed hosts.
        """
        # GIVEN / WHEN a build with two allowed hosts and --verifier-no-network
        self.build("--allow-host", "bedrock-runtime.us-east-1.amazonaws.com",
                   "--allow-host", "registry.npmjs.org", "--verifier-no-network")
        cfg = tomllib.loads((self.out / "task.toml").read_text())
        # THEN verifier is no-network, agent is an allowlist of exactly those hosts
        self.assertEqual(cfg["verifier"]["network_mode"], "no-network")
        self.assertEqual(cfg["agent"]["network_mode"], "allowlist")
        self.assertEqual(cfg["agent"]["allowed_hosts"],
                         ["bedrock-runtime.us-east-1.amazonaws.com", "registry.npmjs.org"])
        self.assertEqual((cfg["version"], cfg["metadata"]["name"]), ("1.0", "demo-task"))
        env = cfg["environment"]
        self.assertEqual((env["cpus"], env["memory_mb"], env["storage_mb"]), (2, 4096, 10240))

    def test_task_toml_without_flags_has_no_network_keys(self):
        """
        Some sandbox providers/tiers reject any per-sandbox network policy, so a
        default build must write no network key anywhere, or the task cannot
        start there at all.
        """
        # GIVEN / WHEN a build without --allow-host or --verifier-no-network
        self.build()
        cfg = tomllib.loads((self.out / "task.toml").read_text())
        # THEN no table has a network key
        for table in cfg.values():
            if isinstance(table, dict):
                self.assertNotIn("network_mode", table)
                self.assertNotIn("allowed_hosts", table)

    def test_dockerfile_unpacks_repo_into_workdir(self):
        """The image must unpack the snapshot into the workdir and run setup there."""
        # GIVEN / WHEN a build with an env var, a setup command and a non-default workdir
        self.build("--env", "CI=1", "--setup", "npm ci", "--workdir", "/srv/code")
        lines = (self.out / "environment/Dockerfile").read_text().splitlines()
        # THEN the Dockerfile has the documented lines in order
        self.assertEqual(lines[0], "FROM node:24-bookworm")
        self.assertIn('ENV CI="1"', lines)
        self.assertLess(lines.index("WORKDIR /srv/code"), lines.index("RUN npm ci"))
        self.assertIn("COPY repo.tar.gz /tmp/repo.tar.gz", lines)
        self.assertIn("RUN mkdir -p /srv/code && tar -xzf /tmp/repo.tar.gz -C /srv/code && rm /tmp/repo.tar.gz",
                      lines)

    def test_dockerfile_env_values_are_escaped(self):
        """
        Docker expands `$VAR` and unescapes `\\` and `\\"` inside a double-quoted
        ENV value, so each must be backslash-escaped or the sandbox gets a
        different value than test.sh exports.
        """
        # GIVEN / WHEN a build with a value holding $, a quote and a backslash
        self.build("--env", 'TOKENISH=a$HOME"b\\c')
        lines = (self.out / "environment/Dockerfile").read_text().splitlines()
        # THEN each is escaped once
        self.assertIn('ENV TOKENISH="a\\$HOME\\"b\\\\c"', lines)

    def test_unsafe_dockerfile_inputs_are_refused(self):
        """A newline would start a new Dockerfile/test.sh instruction; a bad workdir breaks `cd`."""
        cases = (("--env", "A=1\nRUN curl x"), ("--setup", "npm ci\nRUN curl x"),
                 ("--workdir", "/app/my repo"), ("--workdir", "/"), ("--workdir", "/app"),
                 ("--workdir", "/app/$x"))
        for flag, value in cases:
            with self.subTest(flag=flag, value=value):
                # GIVEN / WHEN a build with that input
                p = self.build(flag, value, check=False)
                # THEN refused, nothing written
                self.assertNotEqual(p.returncode, 0)
                self.assertIn(flag, p.stderr)
                self.assertFalse(self.out.exists())
        # AND a newline in the test command is refused too
        p = self.build(command="true\nfalse", check=False)
        self.assertIn("--test-command", p.stderr)
        self.assertFalse(self.out.exists())

    def test_test_sh_reward_one_when_command_passes(self):
        """
        test.sh copies the hidden tests over the workdir and runs the command
        with --env exported; a pass must write reward 1.
        """
        # GIVEN a task whose command checks a hidden test file and an env var
        self.build("--env", "GREETING=hi there",
                   command='test "$(cat tests/app.test.js)" = "new test" && test "$GREETING" = "hi there"')
        work = self.extract_env(self.tmp / "work")
        # WHEN test.sh runs
        reward = self.run_test_sh(work, self.tmp / "v")
        # THEN reward is 1 and the output log exists
        self.assertEqual(reward, "1")
        self.assertTrue((self.tmp / "v/test-output.log").is_file())

    def test_test_sh_reward_zero_when_command_fails(self):
        """A failing test command must write reward 0, never leave the file empty."""
        # GIVEN a task whose command fails
        self.build(command="echo failing; exit 3")
        work = self.extract_env(self.tmp / "work")
        # WHEN test.sh runs / THEN reward is 0 and the output was logged
        self.assertEqual(self.run_test_sh(work, self.tmp / "v"), "0")
        self.assertIn("failing", (self.tmp / "v/test-output.log").read_text())

    def test_test_sh_reward_zero_when_workdir_missing(self):
        """An early failure (no workdir) must still leave reward 0 via the EXIT trap."""
        # GIVEN a task with a passing command
        self.build()
        # WHEN test.sh runs against a workdir that does not exist
        reward = self.run_test_sh(self.tmp / "missing", self.tmp / "v")
        # THEN reward is 0
        self.assertEqual(reward, "0")
        self.assertFalse((self.tmp / "missing").exists())

    def test_existing_out_dir_is_refused_without_force(self):
        """Never overwrite a task folder silently; --force replaces it."""
        # GIVEN an existing non-empty out dir
        self.out.mkdir()
        (self.out / "keep").write_text("x")
        # WHEN built without --force / THEN refused and untouched
        self.assertNotEqual(self.build(check=False).returncode, 0)
        self.assertTrue((self.out / "keep").is_file())
        # WHEN built with --force / THEN replaced
        self.build("--force")
        self.assertFalse((self.out / "keep").exists())
        self.assertTrue((self.out / "task.toml").is_file())

    def manifest_args(self, mpath, title_set="sidecheck"):
        return ("--manifest", str(mpath), "--set", title_set, "--source", "a product repo",
                "--language", "typescript", "--kind", "bugfix", "--visible-test-command", "npm test",
                "--pr", "42", "--repo-slug", "org/product", "--screen")

    def test_manifest_row_is_written_and_loads(self):
        """
        The manifest row is what run_exam reads; it must pass manifest.load and
        carry the visible command, never the hidden one.
        """
        # GIVEN a manifest path that does not exist yet
        mpath = self.tmp / "set" / "sidecheck.json"
        self.out = self.tmp / "set" / "tasks" / "demo-task"
        # WHEN the task is built with --manifest
        self.build(*self.manifest_args(mpath), command="npx vitest run hidden")
        # THEN manifest.load accepts it and the row has the documented fields
        data = manifest.load(mpath, require_cache=True)
        (row,) = data["tasks"]
        self.assertEqual((data["set"], data["source"]), ("sidecheck", "a product repo"))
        self.assertEqual(row["harbor_path"], "tasks/demo-task")
        self.assertEqual((row["repo"], row["pr"], row["kind"], row["screen"]), ("org/product", 42, "bugfix", True))
        self.assertEqual(row["test_command"], "npm test")
        self.assertEqual(row["workdir"], "/app/repo")
        date = run(["git", "log", "-1", "--format=%cs", self.merge], self.repo).stdout.strip()
        self.assertEqual(row["merged_at"], date)

    def test_rebuilding_same_id_replaces_the_manifest_row(self):
        """Rebuilding a task must not duplicate its row (load() rejects duplicate ids)."""
        # GIVEN a manifest with the task already in it
        mpath = self.tmp / "sidecheck.json"
        self.build(*self.manifest_args(mpath))
        # WHEN it is rebuilt with a new title
        self.build("--force", *self.manifest_args(mpath), "--title", "Say v2 again")
        # THEN there is still one row, with the new title
        (row,) = manifest.load(mpath)["tasks"]
        self.assertEqual(row["title"], "Say v2 again")

    def test_visible_command_naming_a_hidden_test_is_refused(self):
        """The visible command must not point the agent at a hidden test file."""
        # GIVEN / WHEN a visible test command naming a hidden test
        mpath = self.tmp / "m.json"
        args = list(self.manifest_args(mpath))
        args[args.index("npm test")] = "npx vitest run tests/app.test.js"
        p = self.build(*args, check=False)
        # THEN refused
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("tests/app.test.js", p.stderr)
        self.assertFalse(mpath.exists())

    def test_manifest_refusals_write_nothing(self):
        """
        Every manifest check runs before the task folder is written, so a
        refused manifest row never leaves a task folder (with hidden tests)
        behind or changes the manifest.
        """
        mpath = self.tmp / "m.json"
        full = list(self.manifest_args(mpath))

        def without(*flags):
            out = list(full)
            for f in flags:
                i = out.index(f)
                del out[i:i + 2]
            return out

        leak = list(full)
        leak[leak.index("npm test")] = "cat tests/new.test.js"
        mount = list(full)
        mount[mount.index("npm test")] = "bash /tests/run.sh"
        cases = {"--visible-test-command naming a hidden file": (leak, "tests/new.test.js"),
                 "--visible-test-command using the /tests/ mount": (mount, "/tests/"),
                 "missing --set": (without("--set"), "--set"),
                 "missing --source": (without("--source"), "--source"),
                 "missing --language": (without("--language"), "--language"),
                 "missing --kind": (without("--kind"), "--kind")}
        for name, (args, needle) in cases.items():
            with self.subTest(name):
                # GIVEN / WHEN a build whose manifest row would be refused
                p = self.build(*args, check=False)
                # THEN refused naming the problem; no task folder and no manifest
                self.assertNotEqual(p.returncode, 0)
                self.assertIn(needle, p.stderr)
                self.assertFalse(self.out.exists())
                self.assertFalse(mpath.exists())
        # AND against an existing manifest, a refused row leaves it byte-identical
        self.build(*full)
        before = mpath.read_bytes()
        self.out = self.tmp / "task2"
        for args in (leak, mount):
            p = self.build(*args, check=False)
            self.assertNotEqual(p.returncode, 0)
            self.assertEqual(mpath.read_bytes(), before)
            self.assertFalse(self.out.exists())
        # AND an existing manifest that is already invalid is refused before any write
        mpath.write_text('{"set": "s", "source": "x", "tasks": [{"id": "bad"}]}')
        before = mpath.read_bytes()
        p = self.build(*full, check=False)
        self.assertIn("nothing written", p.stderr)
        self.assertEqual(mpath.read_bytes(), before)
        self.assertFalse(self.out.exists())

    def test_pr_changing_only_tests_is_refused(self):
        """
        A PR with no non-test change gives an empty fix.patch: the nop control
        would score the same as the oracle, so the task measures nothing.
        """
        # GIVEN a merge commit that only edits a test file
        _, self.merge = make_repo(self.repo, {"tests/app.test.js": "newer test\n"})
        # WHEN built / THEN refused and nothing written
        p = self.build(test_files=("tests/app.test.js",), check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("nothing to solve", p.stderr)
        self.assertFalse(self.out.exists())


if __name__ == "__main__":
    unittest.main()
