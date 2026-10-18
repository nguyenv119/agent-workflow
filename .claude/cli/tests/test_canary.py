"""Tests for `agent-cli harness canary`, with a fake gh on PATH and a local "agent-workflow" source repo whose
loop.py and fetch_cache.py are fakes that log what they were given. No exam, network, Docker or claude runs: the
default public source URL is redirected to the local repo with git's url.<local>.insteadOf (GIT_CONFIG_* env).
"""
import contextlib
import io
import json
import os
import re
import sys
import time
import unittest
import unittest.mock
from pathlib import Path

from helpers import CLAUDE, GIT, TmpCase, agent_cli, make_project, run

sys.path.insert(0, str(CLAUDE / "cli"))
sys.path.insert(0, str(CLAUDE.parent / "harness-loop"))
from agent_cli import canary  # noqa: E402

# REVIEW: mocking core dependency. Why: `gh` talks to GitHub, which no test may reach. Like real gh, `pr view`
# prints ONLY the fields named in --json (from $FAKE_GH_PR) and fails when $FAKE_GH_FAIL is set; every call is
# logged to $FAKE_GH_LOG. `api repos/R/commits/S/status` answers as GitHub's combined status does: the latest
# status per context posted on S, read from that same log. What breaks if it printed every field: canary.py
# could stop asking for isCrossRepository and the fork test would still pass here, while on GitHub every fork PR
# would run; a canned read-back would pass however the posts went.
FAKE_GH = """#!/usr/bin/env python3
import json, os, sys
argv = sys.argv[1:]
log = os.environ["FAKE_GH_LOG"]
path = argv[1].split("?")[0] if len(argv) > 1 else ""  # a read-back carries ?per_page=100
if argv[0] == "api" and path.endswith("/status"):  # read the log before this call is in it
    latest, sha = {}, path.split("/")[-2]
    for c in map(json.loads, open(log).read().splitlines() if os.path.exists(log) else []):
        if c[0] == "api" and c[1].endswith("/statuses/" + sha):
            f = dict(c[i + 1].split("=", 1) for i, x in enumerate(c) if x == "-f")
            latest[f["context"]] = f
with open(log, "a") as fh:
    fh.write(json.dumps(argv) + "\\n")
if os.environ.get("FAKE_GH_FAIL"):
    sys.exit("gh: HTTP 502")
if argv[:2] == ["pr", "view"]:
    fields = argv[argv.index("--json") + 1].split(",") if "--json" in argv else []
    print(json.dumps({k: v for k, v in json.loads(os.environ["FAKE_GH_PR"]).items() if k in fields}))
elif argv[0] == "api" and path.endswith("/status"):
    print(json.dumps({"state": "pending", "statuses": list(reversed(latest.values()))}))
"""
FAKE_LOOP = """import json, os, signal, subprocess, sys, time
from pathlib import Path
argv = sys.argv[1:]
gh_log = Path(os.environ.get("FAKE_GH_LOG", "/nonexistent"))
posted = [a[a.index("-f") + 1] for a in map(json.loads, gh_log.read_text().splitlines()) if "-f" in a] \\
    if gh_log.is_file() else []
cand = Path(argv[argv.index("--candidate-installed") + 1]) if "--candidate-installed" in argv else None
pin = cand / ".claude/HARNESS_VERSION" if cand else None
with open(os.environ["FAKE_LOOP_LOG"], "a") as fh:
    fh.write(json.dumps({"argv": argv, "cwd": os.getcwd(), "home": os.environ.get("HARNESS_LOOP_HOME"),
        "env_file": os.environ.get("HARNESS_EXAM_ENV_FILE"), "posted_before": posted,
        "head": subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip(),
        "cand_pin": json.loads(pin.read_text()) if pin and pin.is_file() else None,
        "cand_files": sorted(p.relative_to(cand).as_posix() for p in cand.rglob("*") if p.is_file()) if cand else None})
        + "\\n")
if os.environ.get("FAKE_LOOP_POST"):  # loop.py posting its verdict before it exits (--pr mode: on gh's head)
    sha = argv[argv.index("--status-sha") + 1] if "--status-sha" in argv else \\
        json.loads(os.environ["FAKE_GH_PR"])["headRefOid"]
    subprocess.run(["gh", "api", "repos/%s/statuses/%s" % (argv[argv.index("--status-repo") + 1], sha),
                    "-f", "state=" + os.environ["FAKE_LOOP_POST"],
                    "-f", "context=harness-canary", "-f", "description=verdict"])
if os.environ.get("FAKE_LOOP_SIGINT_PARENT"):  # Ctrl-C reaching the canary while the exam runs
    os.kill(os.getppid(), signal.SIGINT)
    time.sleep(10)
sys.exit(int(os.environ.get("FAKE_LOOP_EXIT", "0")))
"""
PAUSED = "could not run: paused (usage limit or no plan token); run the canary again"
FAKE_FETCH = """import os, sys
with open(os.environ["FAKE_LOOP_LOG"] + ".fetch", "a") as fh:
    fh.write(" ".join(sys.argv[1:]) + "\\n")
"""
SKILL = "---\nname: {0}\ndescription: Does {0}.\n---\n\n{1}\n"
PENDING = "running since <t>"  # statuses() writes the pending's UTC minute as <t>
RUNNING_SINCE = re.compile(r"running since \d{4}-\d\d-\d\dT\d\d:\d\dZ")


def commit(repo, files, msg="c"):
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
    run(GIT + ["add", "-A"], repo)
    run(GIT + ["commit", "-qm", msg], repo)
    return run(GIT + ["rev-parse", "HEAD"], repo).stdout.strip()


class FakeGhCase(TmpCase):
    """self.bin holding the fake gh, its call log self.gh_log, and the status posts read back from that log."""

    def setUp(self):
        super().setUp()
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        (self.bin / "gh").write_text(FAKE_GH)
        (self.bin / "gh").chmod(0o755)
        self.gh_log = self.tmp / "gh.jsonl"

    def gh_calls(self):
        return [json.loads(x) for x in self.gh_log.read_text().splitlines()] if self.gh_log.is_file() else []

    def reads(self):
        """The combined-status read-backs: one per settle."""
        return [c for c in self.gh_calls() if c[0] == "api" and c[1].endswith("/status?per_page=100")]

    def statuses(self):
        """(path, fields) of each status post, a pending's UTC minute written as <t>."""
        return [(c[1], {k: RUNNING_SINCE.sub(PENDING, v) for k, v in
                        (c[i + 1].split("=", 1) for i, x in enumerate(c) if x == "-f")})
                for c in self.gh_calls() if c[0] == "api" and "-f" in c]

    def status(self, sha, state, desc, repo="owner/app"):
        return (f"repos/{repo}/statuses/{sha}", {"state": state, "context": "harness-canary", "description": desc})


class Canary(FakeGhCase):
    """An installing repo (agent-cli installed, HARNESS_VERSION pinned to the source's newer commit) with a PR."""

    def setUp(self):
        super().setUp()
        self.src = self.tmp / "aw-src"
        self.src.mkdir()
        run(GIT + ["init", "-q"], self.src)
        files = {"harness-loop/loop.py": FAKE_LOOP, "harness-loop/tasks/fetch_cache.py": FAKE_FETCH}
        self.old = commit(self.src, files)
        self.new = commit(self.src, {"README.md": "newer\n"})
        self.app = make_project(self.tmp / "app")
        self.origin = self.tmp / "app-origin.git"
        run(GIT + ["init", "-q", "--bare", str(self.origin)], self.tmp)
        self.pin = {"commit": self.new, "addon": ".claude/harness-addon", "addon_sha256": "a" * 64}
        commit(self.app, {".claude/HARNESS_VERSION": json.dumps(self.pin) + "\n", "AGENTS.md": "# Agents\n",
                          ".claude/skills/implementer/SKILL.md": SKILL.format("implementer", "Build."),
                          ".claude/skills/deploy/SKILL.md": SKILL.format("deploy", "Ship it."),
                          ".claude/harness-addon/skills/implementer/SKILL.md": SKILL.format("implementer", "Build."),
                          ".claude/project-rules.md": "## harness\nexam-env-file: ~/.config/harness/exam.env\n"})
        run(GIT + ["remote", "add", "origin", str(self.origin)], self.app)
        run(GIT + ["push", "-q", "origin", "HEAD:main"], self.app)
        self.user = self.tmp / "user"
        self.user.mkdir()
        self.loop_log = self.tmp / "loop.jsonl"

    def pr(self, files, cross=False):
        """Push a PR head (refs/pull/7/head) off main changing files; set the fake gh's pr view; return its sha."""
        run(GIT + ["checkout", "-q", "-b", "pr-7"], self.app)
        head = commit(self.app, files, "pr")
        run(GIT + ["push", "-q", "origin", "HEAD:refs/pull/7/head"], self.app)
        run(GIT + ["checkout", "-q", "-"], self.app)
        self.pr_json = json.dumps({"isCrossRepository": cross, "headRepositoryOwner": {"login": "owner"},
                                   "headRefOid": head, "baseRefName": "main",
                                   "url": "https://github.com/owner/app/pull/7"})
        return head

    def main_moves(self, files):
        """Commit files on the app's main and push it (main moved after the PR branched)."""
        commit(self.app, files, "main moves")
        run(GIT + ["push", "-q", "origin", "HEAD:main"], self.app)

    def canary(self, *args, env=None):
        e = {"PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}", "HOME": str(self.user),
             "FAKE_GH_LOG": str(self.gh_log), "FAKE_LOOP_LOG": str(self.loop_log),
             "FAKE_GH_PR": getattr(self, "pr_json", "{}"), "HARNESS_CANARY_SOURCE": str(self.src),
             "HARNESS_LOOP_HOME": None, "HARNESS_EXAM_ENV_FILE": None, "HARNESS_CANARY_REF": None,
             "CLAUDE_CODE_REMOTE": None, **(env or {})}
        return agent_cli(self.app, "harness", "canary", *args, env=e)

    def loops(self):
        return [json.loads(x) for x in self.loop_log.read_text().splitlines()] if self.loop_log.is_file() else []

    def fetched(self):
        return Path(str(self.loop_log) + ".fetch").exists()

    def tree(self, name, commit_sha):
        d = self.tmp / name
        (d / ".claude/skills/implementer").mkdir(parents=True)
        (d / ".claude/skills/implementer/SKILL.md").write_text(SKILL.format("implementer", name))
        (d / "AGENTS.md").write_text("# Agents\n")
        (d / ".claude/HARNESS_VERSION").write_text(json.dumps({**self.pin, "commit": commit_sha}))
        return d

    def test_work_loaded_matches_loop_py(self):
        """
        What: canary.py's copies of WORK_LOADED, FRONTMATTER_FILE and the status context equal harness-loop/loop.py's.
        Why: agent-cli must gate before cloning, so it cannot import loop.py.
        Breaks: the two gates disagree, or agent-cli and loop.py post under two status names.
        """
        # GIVEN loop.py importable from the source tree
        import loop
        # WHEN / THEN the copies are equal
        self.assertEqual(canary.WORK_LOADED, loop.WORK_LOADED)
        self.assertEqual(canary.FRONTMATTER_FILE.pattern, loop.FRONTMATTER_FILE.pattern)
        self.assertEqual(canary.CONTEXT, loop.CANARY_CONTEXT)

    def test_help_summary_is_one_line_and_the_subcommand_carries_prereqs_and_exits(self):
        """
        What: `agent-cli harness --help` shows canary's summary on one line; `harness canary --help` carries the
        prerequisites and the exit table (with 3 for gh trouble).
        Why: the command list is scanned, the subcommand help is read. Breaks: a ten-line entry buries drift.
        """
        # GIVEN the installed agent-cli
        # WHEN both helps print
        top = self.canary("--help").stdout
        listing = agent_cli(self.app, "harness", "--help").stdout.splitlines()
        # THEN the canary row is one line, and the subcommand help names Docker and every exit code
        (i,) = [n for n, line in enumerate(listing) if line.strip().startswith("canary ")]
        self.assertFalse(listing[i + 1].startswith(" " * 8), listing[i:i + 2])
        for part in ("Docker", "CLAUDE_CODE_OAUTH_TOKEN", "pending", "0 pass", "1 fail", "3 gh", "4 paused",
                     "5 could not run"):
            self.assertIn(part, " ".join(top.split()))

    def test_default_clone_source_is_the_public_url(self):
        """
        What: with no HARNESS_CANARY_SOURCE, the clone URL is the public agent-workflow repo (no auth).
        Why: an installing repo has no other copy. The URL is redirected to the local repo by git config, so
        the test proves the URL used without touching the network.
        Breaks: an installing repo clones a private or wrong source.
        """
        # GIVEN two trees and the public URL redirected to the local source
        self.assertEqual(canary.PUBLIC_SOURCE, "https://github.com/nguyenv119/agent-workflow")
        cand, base = self.tree("cand", self.old), self.tree("base", self.old)
        redirect = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": f"url.{self.src}.insteadOf",
                    "GIT_CONFIG_VALUE_0": canary.PUBLIC_SOURCE, "HARNESS_CANARY_SOURCE": None}
        # WHEN the canary runs in no-PR mode
        r = self.canary("--candidate", str(cand), "--baseline", str(base), env=redirect)
        # THEN it cloned the public URL at the running install's commit and ran loop.py once
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(f"source {canary.PUBLIC_SOURCE} ref {self.new}", r.stdout)
        self.assertEqual(len(self.loops()), 1)

    def test_no_pr_mode_clones_the_running_install_commit_not_the_trees_pins(self):
        """
        What: --candidate/--baseline trees pinned to an older commit still clone the running install's commit,
        and loop.py gets both trees in installed mode with no status flags; agent-cli fetches no tasks itself.
        Why: older pins may predate the canary flags; fetching is loop.py's exam setup.
        Breaks: loop.py rejects --candidate-installed, or tasks download before any gate.
        """
        # GIVEN two trees pinned to the older commit
        cand, base = self.tree("cand", self.old), self.tree("base", self.old)
        # WHEN the canary runs in no-PR mode
        r = self.canary("--candidate", str(cand), "--baseline", str(base))
        # THEN loop.py at the newer commit got both trees and the canary flags, and nothing else ran
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        (call,) = self.loops()
        self.assertEqual(call["head"], self.new)
        a = call["argv"]
        self.assertEqual(a[:5], ["exam", "--candidate-installed", str(cand), "--baseline-installed", str(base)])
        self.assertIn("--subscription-only", a)
        self.assertEqual(a[a.index("--rerun-on-fail") + 1], "1")
        self.assertNotIn("--post-status", a)
        self.assertEqual(self.gh_calls(), [])
        self.assertFalse(self.fetched())

    def test_installing_repo_defaults_loop_home_private_and_passes_the_env_file(self):
        """
        What: any installing repo (add-on or not) gets HARNESS_LOOP_HOME=~/.harness-loop-private/<repo>, and
        project-rules' exam-env-file becomes HARNESS_EXAM_ENV_FILE (~ expanded); an inherited HARNESS_LOOP_HOME wins.
        Why: an installing repo's trees are its own; ~/.harness-loop's runs/ feed agent-workflow's public improver.
        Breaks: a private repo's runs land where a public PR can quote them.
        """
        cand, base = self.tree("cand", self.old), self.tree("base", self.old)
        for addon in (self.pin["addon"], None):
            with self.subTest(addon=addon):
                # GIVEN an install with (or without) an add-on
                (self.app / ".claude/HARNESS_VERSION").write_text(json.dumps({**self.pin, "addon": addon}))
                self.loop_log.unlink(missing_ok=True)
                # WHEN the canary runs, then again with HARNESS_LOOP_HOME inherited
                self.assertEqual(self.canary("--candidate", str(cand), "--baseline", str(base)).returncode, 0)
                self.canary("--candidate", str(cand), "--baseline", str(base),
                            env={"HARNESS_LOOP_HOME": str(self.tmp / "mine")})
                # THEN the private default, the env file, and the inherited home winning
                first, second = self.loops()
                self.assertEqual(first["home"], str(self.user / ".harness-loop-private/app"))
                self.assertEqual(first["env_file"], str(self.user / ".config/harness/exam.env"))
                self.assertEqual(second["home"], str(self.tmp / "mine"))

    def test_pr_touching_only_a_project_skill_is_not_needed_and_clones_nothing(self):
        """
        What: a PR changing the body of a project skill /work never loads posts success "not needed (no loaded
        file)" on the PR head in the PR's repo (after the run's pending), exit 0, with no clone, fetch or exam.
        Why: the merge gate needs the status on every PR. Breaks: a docs-only PR waits on a laptop.
        """
        # GIVEN a PR changing only a project skill's body
        head = self.pr({".claude/skills/deploy/SKILL.md": SKILL.format("deploy", "Ship it carefully.")})
        # WHEN the canary runs
        r = self.canary("--pr", "7")
        # THEN pending, then success "not needed" on the head, nothing cloned, fetched or run
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.statuses(), [self.status(head, "pending", PENDING),
                                           self.status(head, "success", "not needed (no loaded file)")])
        self.assertEqual(self.loops(), [])
        self.assertFalse(self.fetched())

    def test_harness_version_bump_is_needed_and_runs_the_pr_trees(self):
        """
        What: a PR bumping .claude/HARNESS_VERSION runs the exam: clone at the PR head's pinned commit, the PR
        head's .claude + AGENTS.md as the candidate, status flags for THIS repo's PR head.
        Why: a bump changes every installed file. Breaks: bumps merge untested.
        """
        # GIVEN a PR pinning the older source commit
        head = self.pr({".claude/HARNESS_VERSION": json.dumps({**self.pin, "commit": self.old}) + "\n"})
        # WHEN the canary runs and loop.py fails
        r = self.canary("--pr", "7", env={"FAKE_LOOP_EXIT": "1", "FAKE_LOOP_POST": "failure"})
        # THEN loop.py's exit passes through; it ran at the PR's pin with the PR's tree and this repo's status
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        (call,) = self.loops()
        self.assertEqual(call["head"], self.old)
        self.assertEqual(call["cand_pin"]["commit"], self.old)
        self.assertIn("AGENTS.md", call["cand_files"])
        a = call["argv"]
        self.assertEqual((a[a.index("--status-repo") + 1], a[a.index("--status-sha") + 1]), ("owner/app", head))
        self.assertIn("--post-status", a)
        self.assertEqual(self.statuses(), [self.status(head, "pending", PENDING),
                                           self.status(head, "failure", "verdict")])  # posted by loop.py
        self.assertFalse(self.fetched())

    def test_pr_is_scored_as_it_would_land_on_main(self):
        """
        What: when main moved after the PR branched, the candidate tree is the PR merged onto main (git
        merge-tree), holding main's newer files too.
        Why: loop.py's --pr exam restacks the same way; a PR must be scored as it would land.
        Breaks: a PR is scored on a stale base, missing whatever main changed since.
        """
        # GIVEN a PR bumping the pin, then main adding a skill
        self.pr({".claude/HARNESS_VERSION": json.dumps({**self.pin, "commit": self.old}) + "\n"})
        self.main_moves({".claude/skills/quick/SKILL.md": SKILL.format("quick", "Quick.")})
        # WHEN the canary runs
        r = self.canary("--pr", "7")
        # THEN the candidate holds both the PR's pin and main's new skill
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        (call,) = self.loops()
        self.assertEqual(call["cand_pin"]["commit"], self.old)
        self.assertIn(".claude/skills/quick/SKILL.md", call["cand_files"])

    def test_pr_that_conflicts_with_main_is_stale(self):
        """
        What: a PR that does not merge cleanly onto main posts failure "stale, rebase the PR onto main" and exits 1,
        with no clone or exam.
        Why: the same outcome as loop.py's free check. Breaks: a conflicting PR scored on its own stale tree.
        """
        # GIVEN a PR and main both rewriting AGENTS.md
        head = self.pr({"AGENTS.md": "# Agents v2\n"})
        self.main_moves({"AGENTS.md": "# Agents on main\n"})
        # WHEN the canary runs
        r = self.canary("--pr", "7")
        # THEN failure "stale", exit 1, nothing run
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(self.statuses(), [self.status(head, "pending", PENDING),
                                           self.status(head, "failure", "stale, rebase the PR onto main")])
        self.assertIn("stale, rebase the PR onto main", r.stderr)
        self.assertEqual(self.loops(), [])

    def test_addon_source_edit_is_needed(self):
        """
        What: editing the add-on's copy of a loaded skill (<addon>/skills/implementer/) needs the exam.
        Why: the add-on's files install over the base skill. Breaks: add-on edits merge untested.
        """
        # GIVEN a PR editing the add-on's implementer skill
        self.pr({".claude/harness-addon/skills/implementer/SKILL.md": SKILL.format("implementer", "Build fast.")})
        # WHEN the canary runs with --no-status
        r = self.canary("--pr", "7", "--no-status")
        # THEN loop.py ran, without status flags
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(len(self.loops()), 1)
        self.assertNotIn("--post-status", self.loops()[0]["argv"])

    def test_pending_is_posted_before_the_exam_runs(self):
        """
        What: a --pr run posts pending "running since <UTC minute>" on the PR head before loop.py starts.
        Why: `agent-cli pr wait` keeps a harness PR WAITING while the status is missing or pending; with only final
        states, "missing" could not tell "nobody runs it" from "it is running", and the start time lets a reader
        tell a killed run's pending from a live one. Breaks: a running canary reads as one nobody started, or a
        killed one blocks the PR forever.
        """
        # GIVEN a PR that needs the exam, and an exam that posts its verdict
        head = self.pr({"AGENTS.md": "# Agents v2\n"})
        before = time.time()
        # WHEN the canary runs
        r = self.canary("--pr", "7", env={"FAKE_LOOP_POST": "success"})
        # THEN loop.py started after pending was on GitHub, stamped with the current UTC minute
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        (call,) = self.loops()
        self.assertEqual(call["posted_before"], ["state=pending"])
        desc = dict(x.split("=", 1) for x in self.gh_calls()[1] if "=" in x)["description"]
        self.assertRegex(desc, RUNNING_SINCE)
        stamp = time.mktime(time.strptime(desc[len("running since "):], "%Y-%m-%dT%H:%MZ")) - time.timezone
        self.assertLess(abs(stamp - before), 120, desc)
        self.assertEqual(self.statuses(), [self.status(head, "pending", PENDING),
                                           self.status(head, "success", "verdict")])

    def test_a_verdict_loop_py_posted_is_left_alone_whatever_its_exit(self):
        """
        What: when loop.py posted its verdict (success, failure or error) the canary reads it back and posts
        nothing more, and passes loop.py's exit through.
        Why: the read-back, not the exit code, says whether pending is still there. Breaks: an error overwrites
        loop.py's real failure or pass.
        """
        head = self.pr({"AGENTS.md": "# Agents v2\n"})
        for code, state in ((0, "success"), (1, "failure"), (5, "error")):
            with self.subTest(exit=code):
                # GIVEN an exam that posts this verdict and exits with this code
                self.gh_log.unlink(missing_ok=True)
                # WHEN the canary runs
                r = self.canary("--pr", "7", env={"FAKE_LOOP_EXIT": str(code), "FAKE_LOOP_POST": state})
                # THEN pending, the verdict, one read-back, nothing after; the code passes through
                self.assertEqual(r.returncode, code, r.stdout + r.stderr)
                self.assertEqual(self.statuses(), [self.status(head, "pending", PENDING),
                                                   self.status(head, state, "verdict")])
                self.assertEqual(len(self.reads()), 1)

    def test_exam_exit_that_leaves_pending_replaces_it_with_error(self):
        """
        What: loop.py exiting without posting (1 from an uncaught exception, 5 with no target, 4 paused) leaves
        pending on the head, so the canary posts error "could not run: <reason>"; the exit code passes through.
        Why: loop.py posts nothing on those exits. Breaks: `pr wait` sees a canary "running" forever and idles to
        its cap.
        """
        head = self.pr({"AGENTS.md": "# Agents v2\n"})
        for code, desc in ((1, "could not run: the exam exited 1"), (5, "could not run: the exam exited 5"),
                           (4, PAUSED)):
            with self.subTest(exit=code):
                # GIVEN an exam ending with this code and no verdict posted
                self.gh_log.unlink(missing_ok=True)
                # WHEN the canary runs
                r = self.canary("--pr", "7", env={"FAKE_LOOP_EXIT": str(code)})
                # THEN pending, then error with the reason; the code passes through
                self.assertEqual(r.returncode, code, r.stdout + r.stderr)
                self.assertEqual(self.statuses(), [self.status(head, "pending", PENDING),
                                                   self.status(head, "error", desc)])

    def test_an_error_after_pending_replaces_it_with_error(self):
        """
        What: an exception after pending and before the exam (the PR's .claude/HARNESS_VERSION is not JSON) posts
        error "could not run: the canary stopped on an error" and still fails the command.
        Why: every way out after pending must settle it. Breaks: a crash leaves pending, and the PR waits forever.
        """
        # GIVEN a PR whose pin is not JSON
        head = self.pr({".claude/HARNESS_VERSION": "not json\n"})
        # WHEN the canary runs
        r = self.canary("--pr", "7")
        # THEN the exception surfaces, nothing ran, and pending became an error
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("JSONDecodeError", r.stderr)
        self.assertEqual(self.loops(), [])
        self.assertEqual(self.statuses(), [self.status(head, "pending", PENDING),
                                           self.status(head, "error",
                                                       "could not run: the canary stopped on an error")])

    def test_ctrl_c_during_the_exam_replaces_pending_with_error_interrupted(self):
        """
        What: a Ctrl-C (SIGINT) reaching agent-cli while loop.py runs posts error "could not run: interrupted",
        and the interrupt still ends the command.
        Why: the run posted pending; a killed run must not leave it. Breaks: `pr wait` waits on a dead canary.
        """
        # GIVEN a PR that needs the exam and a loop.py that sends SIGINT to agent-cli
        head = self.pr({"AGENTS.md": "# Agents v2\n"})
        # WHEN the canary runs
        r = self.canary("--pr", "7", env={"FAKE_LOOP_SIGINT_PARENT": "1"})
        # THEN the interrupt surfaced, and pending became error "interrupted"
        self.assertIn("KeyboardInterrupt", r.stderr)
        self.assertEqual(self.statuses(), [self.status(head, "pending", PENDING),
                                           self.status(head, "error", "could not run: interrupted")])

    def test_no_status_posts_and_reads_nothing_even_when_the_exam_pauses(self):
        """
        What: --no-status posts neither pending nor the paused error, and never reads the status back.
        Why: --no-status is for trial runs on a PR whose status must not change. Breaks: a trial run leaves a
        status on someone's PR.
        """
        # GIVEN a PR that needs the exam and an exam that pauses
        self.pr({"AGENTS.md": "# Agents v2\n"})
        # WHEN the canary runs with --no-status
        r = self.canary("--pr", "7", "--no-status", env={"FAKE_LOOP_EXIT": "4"})
        # THEN exit 4, nothing posted, nothing read
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertEqual((self.statuses(), self.reads()), ([], []))

    def test_settle_never_raises_when_github_cannot_be_read(self):
        """
        What: settle with a gh that fails prints one warning and returns, posting nothing.
        Why: settle runs in a finally; raising there would replace the exam's exit code or exception.
        Breaks: a GitHub outage turns a passed exam into a crash.
        """
        # GIVEN a gh that fails every call
        env = {"PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}", "FAKE_GH_LOG": str(self.gh_log),
               "FAKE_GH_FAIL": "1"}
        stderr = io.StringIO()
        # WHEN settle runs
        with unittest.mock.patch.dict(os.environ, env), contextlib.redirect_stderr(stderr):
            canary.settle(("owner/app", "a" * 40), "the exam exited 1")
        # THEN one warning line, one read attempt, no post
        self.assertEqual(len(stderr.getvalue().splitlines()), 1, stderr.getvalue())
        self.assertIn("warning", stderr.getvalue())
        self.assertEqual((len(self.reads()), self.statuses()), (1, []))

    def test_laptop_only_exits_5_before_gh_or_clone(self):
        """
        What: CLAUDE_CODE_REMOTE set exits 5 "laptop-only" with no gh call, no status and no exam.
        Why: a cloud session has no Docker. Breaks: an error status from a session that should never try.
        """
        # GIVEN a PR that needs the exam
        self.pr({"AGENTS.md": "# Agents v2\n"})
        # WHEN the canary runs in a cloud session
        r = self.canary("--pr", "7", env={"CLAUDE_CODE_REMOTE": "true"})
        # THEN exit 5, no gh call and no loop.py
        self.assertEqual(r.returncode, 5, r.stdout + r.stderr)
        self.assertIn("laptop-only", r.stderr)
        self.assertEqual((self.gh_calls(), self.loops()), ([], []))

    def test_fork_pr_exits_5_with_no_status_and_no_clone(self):
        """
        What: a cross-repository PR exits 5 "fork PR", posts nothing, clones and runs nothing.
        Why: the sandbox holds the plan token and runs the PR's harness. The fake gh answers only the --json
        fields asked for, so this fails if canary.py stops asking for isCrossRepository.
        Breaks: a fork's harness runs with the plan token.
        """
        # GIVEN a PR from a fork
        self.pr({"AGENTS.md": "# Agents v2\n"}, cross=True)
        # WHEN the canary runs
        r = self.canary("--pr", "7")
        # THEN exit 5, nothing posted, cloned or run
        self.assertEqual(r.returncode, 5, r.stdout + r.stderr)
        self.assertIn("fork PR, not run with the plan token", r.stderr)
        self.assertEqual((self.statuses(), self.loops()), ([], []))

    def test_gh_pr_view_failure_exits_3(self):
        """
        What: `gh pr view` failing exits 3 (blocked by gh, as agent-cli pr does), with no clone or exam.
        Why: gh trouble is outside the command and worth a retry. Breaks: exit 1 reads as a failed PR.
        """
        # GIVEN a PR and a gh that fails
        self.pr({"AGENTS.md": "# Agents v2\n"})
        # WHEN the canary runs
        r = self.canary("--pr", "7", env={"FAKE_GH_FAIL": "1"})
        # THEN exit 3 naming gh, nothing run
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn("gh pr view", r.stderr)
        self.assertEqual(self.loops(), [])

    def test_git_fetch_failure_posts_error_and_exits_5(self):
        """
        What: `git fetch` of the PR failing posts state error "could not run: ..." on the PR head and exits 5.
        Why: an unreachable origin is no verdict on the PR. Breaks: exit 1, a red failure on a good PR.
        """
        # GIVEN a PR and an origin that is gone
        head = self.pr({"AGENTS.md": "# Agents v2\n"})
        run(GIT + ["remote", "set-url", "origin", str(self.tmp / "gone.git")], self.app)
        # WHEN the canary runs
        r = self.canary("--pr", "7")
        # THEN exit 5 with an error status on the head, nothing run
        self.assertEqual(r.returncode, 5, r.stdout + r.stderr)
        pending, status = self.statuses()
        self.assertEqual(pending, self.status(head, "pending", PENDING))
        self.assertEqual((status[0], status[1]["state"]), (f"repos/owner/app/statuses/{head}", "error"))
        self.assertTrue(status[1]["description"].startswith("could not run: "), status)
        self.assertEqual(self.loops(), [])

    def test_git_diff_failure_in_the_gate_posts_error_and_exits_5(self):
        """
        What: the path gate's `git diff` / `git merge-base` failing (a base sha not in the local repo) posts state
        error "could not run: git diff failed" on the PR head and exits 5.
        Why: a git failure is no verdict on the PR; the canary's other git failures already exit 5 with an error
        status. Breaks: exit 1 with no status, so the PR's harness-canary check never resolves.
        """
        # GIVEN a PR head, a base sha the repo does not hold, and the fake gh on PATH
        head = self.pr({"AGENTS.md": "# Agents v2\n"})
        env = {"PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}", "FAKE_GH_LOG": str(self.gh_log)}
        stderr = io.StringIO()
        # WHEN the gate runs on them
        with unittest.mock.patch.dict(os.environ, env), contextlib.redirect_stderr(stderr), \
                self.assertRaises(SystemExit) as cm:
            canary.needed(self.app, "0" * 40, head, ("owner/app", head))
        # THEN exit 5 with an error status on the head
        self.assertEqual(cm.exception.code, 5, stderr.getvalue())
        self.assertIn("could not run: git diff failed", stderr.getvalue())
        self.assertEqual(self.statuses(), [self.status(head, "error", "could not run: git diff failed")])

    def test_pr_moved_while_fetching_exits_5(self):
        """
        What: gh naming another head than the one fetched exits 5 "moved while fetching", nothing run.
        Why: the verdict must be for the sha it is posted on; a retry picks up the new head.
        Breaks: a status for one commit computed on another.
        """
        # GIVEN gh reporting a head the fetch does not find
        self.pr({"AGENTS.md": "# Agents v2\n"})
        self.pr_json = json.dumps({**json.loads(self.pr_json), "headRefOid": "0" * 40})
        # WHEN the canary runs
        r = self.canary("--pr", "7")
        # THEN exit 5, nothing run
        self.assertEqual(r.returncode, 5, r.stdout + r.stderr)
        self.assertIn("moved while fetching", r.stderr)
        self.assertEqual(self.loops(), [])

    def test_usage_errors_exit_2(self):
        """
        What: neither --pr nor both trees, or only one tree, is a usage error naming a correct call.
        Why: cli-for-agents: fail fast with an actionable example. Breaks: a half-specified run guesses.
        """
        for args in ((), ("--candidate", "x"), ("--pr", "7", "--candidate", "x", "--baseline", "y")):
            with self.subTest(args=args):
                # WHEN the canary runs with these flags
                r = self.canary(*args)
                # THEN exit 2 with an example
                self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                self.assertIn("example: agent-cli harness canary", r.stderr)


class SourceTree(FakeGhCase):
    """agent-workflow's own checkout (no HARNESS_VERSION): its local loop.py, nothing cloned."""

    def setUp(self):
        """agent-workflow's checkout with fake loop.py and fetch_cache.py, a fake gh, and a same-repo PR 9."""
        super().setUp()
        self.app = make_project(self.tmp / "aw")
        (self.app / "harness-loop/tasks").mkdir(parents=True)
        (self.app / "harness-loop/loop.py").write_text(FAKE_LOOP)
        (self.app / "harness-loop/tasks/fetch_cache.py").write_text(FAKE_FETCH)
        self.log = self.tmp / "loop.jsonl"
        self.pr = {"isCrossRepository": False, "headRefOid": "f" * 40, "baseRefName": "main",
                   "url": "https://github.com/nguyenv119/agent-workflow/pull/9"}

    def canary(self, *args, env=None):
        return agent_cli(self.app, "harness", "canary", "--pr", "9", *args,
                         env={"PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}", "FAKE_GH_LOG": str(self.gh_log),
                              "FAKE_LOOP_LOG": str(self.log), "FAKE_GH_PR": json.dumps(self.pr),
                              "CLAUDE_CODE_REMOTE": None, **(env or {})})

    def loops(self):
        return [json.loads(x) for x in self.log.read_text().splitlines()]

    def status(self, state, desc):
        return super().status("f" * 40, state, desc, "nguyenv119/agent-workflow")

    def test_pr_runs_the_local_loop_with_the_canary_flags(self):
        """
        What: --pr N runs the local `loop.py exam --pr N --tasks tasks/canary-2026-10.json --subscription-only
        --rerun-on-fail 1 --post-status --status-repo <repo>`, and no fetch_cache.py of its own.
        Why: agent-workflow's own PRs are exam-ed from source; loop.py fetches after its gate.
        Breaks: a clone of the public repo instead, or a download before the gate.
        """
        # GIVEN agent-workflow's checkout (setUp)
        # WHEN the canary runs
        r = self.canary()
        # THEN the local loop.py got the canary flags, from harness-loop/, and nothing was fetched
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        (call,) = self.loops()
        self.assertEqual(call["argv"], ["exam", "--pr", "9", "--post-status", "--status-repo",
                                        "nguyenv119/agent-workflow", "--tasks", "tasks/canary-2026-10.json",
                                        "--subscription-only", "--rerun-on-fail", "1"])
        self.assertEqual(Path(call["cwd"]).resolve(), (self.app / "harness-loop").resolve())
        self.assertFalse(Path(str(self.log) + ".fetch").exists())

    def test_pending_is_posted_on_the_pr_head_before_the_local_loop_runs(self):
        """
        What: in the source tree too, pending "running since ..." lands on the PR head before loop.py starts, and
        an exam that posts its verdict gets nothing after it.
        Why: agent-workflow's own harness PRs wait on the same canary. Breaks: its PRs read "nobody runs it".
        """
        # GIVEN agent-workflow's checkout (setUp), and an exam that posts success
        # WHEN the canary runs
        r = self.canary(env={"FAKE_LOOP_POST": "success"})
        # THEN pending was on GitHub when loop.py started, on PR 9's head in its repo; then only the verdict
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        (call,) = self.loops()
        self.assertEqual(call["posted_before"], ["state=pending"])
        self.assertEqual(self.statuses(), [self.status("pending", PENDING), self.status("success", "verdict")])

    def test_local_loop_paused_replaces_pending_with_error(self):
        """
        What: the local loop.py exiting 4 (paused) posts error "could not run: paused ..." and exits 4.
        Why: loop.py posts nothing on a pause, so the run's pending would stay. Breaks: `pr wait` idles to its cap.
        """
        # GIVEN agent-workflow's checkout and an exam that pauses
        # WHEN the canary runs
        r = self.canary(env={"FAKE_LOOP_EXIT": "4"})
        # THEN pending, then the paused error, exit 4
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertEqual(self.statuses(), [self.status("pending", PENDING), self.status("error", PAUSED)])


if __name__ == "__main__":
    unittest.main()
