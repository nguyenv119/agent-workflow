"""Tests for `agent-cli hook scan`: every hook rule run over script files or piped commands."""
import datetime
import json
import os
import time
import unittest
from pathlib import Path

from helpers import TmpCase, agent_cli, make_project

# One file per built-in rule (expected rule id, file name, text).
HITS = [
    ("hand-pr-poll", "pr_wait.sh", "#!/bin/bash\nwhile true; do\n  gh pr checks 12 && break\n  sleep 30\ndone\n"),
    ("agent-cli-hand-verdict", "verdict.sh", '#!/bin/bash\nset -e\necho "<promise>WIN</promise>"\n'),
    ("hand-worktree-add", "wt.sh", "git worktree add -b feat ../wt origin/main\n"),
    ("hand-worktree-add-other", "wt_other.sh", "git worktree add ../wt existing-branch\n"),
    ("hand-sleep-poll", "wait_flag.sh", "until test -f done.flag; do\n  sleep 30\ndone\n"),
    ("hand-loop-log", "log.sh", 'echo "step done" >> .claude/loop-x.log\n'),
]
# Near misses: each looks like a rule's chore but is not one.
NEAR = {
    "quoted.sh": 'echo "while true; do gh pr checks 12; sleep 30; done"\n',
    "short_sleep.sh": "while true; do sleep 1; done\n",
    "detach.sh": "git worktree add --detach ../wt HEAD\n",
    "for_list.sh": "for n in 1 2 3; do gh pr checks $n; sleep 30; done\n",
}


class ScanCase(TmpCase):
    def setUp(self):
        super().setUp()
        self.p = make_project(self.tmp / "proj")
        self.tree = self.p / "evals"
        self.tree.mkdir()

    def write(self, rel, text, age_days=0):
        f = self.tree / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text) if isinstance(text, str) else f.write_bytes(text)
        if age_days:
            t = time.time() - age_days * 86400
            os.utime(f, (t, t))
        return f

    def at(self, rel, day, hour=12):
        """Write a worktree-add hit to rel with its mtime set to local `day` (YYYY-MM-DD) at `hour`:00."""
        f = self.write(rel, "git worktree add -b x ../x\n")
        t = datetime.datetime.strptime(f"{day} {hour}", "%Y-%m-%d %H").timestamp()
        os.utime(f, (t, t))
        return f

    def scan(self, *args, stdin=None):
        r = agent_cli(self.p, "hook", "scan", *args, "--json", stdin=stdin)
        rows = [json.loads(ln) for ln in r.stdout.splitlines()]
        return r, rows[:-1], rows[-1] if rows else None


class Files(ScanCase):
    def test_each_rule_hits_its_file_and_near_misses_do_not(self):
        """Every built-in rule finds its hand-rolled file; quoted text, short sleeps, --detach and `for`
        lists stay clean.

        The weekly count is only useful if each rule fires on the chore and not on look-alikes.
        """
        # GIVEN one file per rule plus near-miss files
        for _, name, text in HITS:
            self.write(name, text)
        for name, text in NEAR.items():
            self.write(name, text)
        # WHEN the tree is scanned
        r, hits, summary = self.scan(str(self.tree))
        # THEN exit 0, each rule hits exactly its own file, and the near misses never appear
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(sorted((h["rule"], Path(h["file"]).name) for h in hits),
                         sorted((rule, name) for rule, name, _ in HITS))
        self.assertEqual((summary["files_scanned"], summary["unreadable"]), (len(HITS) + len(NEAR), 0))
        self.assertEqual(summary["hits_by_rule"], {rule: 1 for rule, _, _ in HITS})
        self.assertEqual(summary["bypassed"], 0)

    def test_hit_row_has_first_matching_line_mode_and_mtime(self):
        """A hit names the line its loop keyword is on, counted in the raw file past a heredoc body and
        past VAR= lines.

        Rules match a view with heredoc bodies removed, and a match can start at an earlier separator;
        the line must still point at the real loop line.
        """
        # GIVEN a sleep loop on line 9, after a heredoc whose body holds a decoy loop and two VAR= lines
        text = ("#!/bin/bash\n# header\ncat <<EOF\nwhile true; do\nsleep 50\nEOF\nN=0\nMAX=10\n"
                "until test -f x; do\n  sleep 30\ndone\n")
        self.write("late.sh", text)
        # WHEN scanned
        _, hits, _ = self.scan(str(self.tree))
        # THEN one hit on line 9 with the rule's mode and an ISO mtime
        self.assertEqual(len(hits), 1)
        h = hits[0]
        self.assertEqual((h["rule"], h["mode"], h["line"], h["bypassed"]), ("hand-sleep-poll", "warn", 9, False))
        self.assertRegex(h["mtime"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")

    def test_bypass_comment_is_still_a_hit_flagged_bypassed(self):
        """`# agent-cli: allow <rule>` does not hide a hit: it is reported with bypassed true and counted.

        A model told to hand-roll a loop appended the allow comment to get past the deny; for this count
        that is "the command was not used", not compliance.
        """
        # GIVEN a worktree add carrying the allow comment, and a plain one
        self.write("bypass.sh", "git worktree add -b x ../x  # agent-cli: allow hand-worktree-add\n")
        self.write("plain.sh", "git worktree add -b y ../y\n")
        # WHEN scanned
        _, hits, summary = self.scan(str(self.tree))
        # THEN both hit, only the commented one is bypassed, and the summary counts it
        by_name = {Path(h["file"]).name: h for h in hits}
        self.assertEqual(by_name["bypass.sh"]["bypassed"], True)
        self.assertEqual(by_name["plain.sh"]["bypassed"], False)
        self.assertEqual(summary["hits_by_rule"], {"hand-worktree-add": 2})
        self.assertEqual(summary["bypassed"], 1)

    def test_since_glob_and_skip_dirs_limit_what_is_scanned(self):
        """Old files, other extensions, skip dirs, binaries and files over 1 MB are not scanned.

        The gardener counts new files each week; old files and vendored or evidence copies would swamp it.
        """
        # GIVEN a fresh hit, an old hit, a .txt hit and hits under every skip dir, plus binary and huge files
        wt = "git worktree add -b x ../x\n"
        self.write("new.sh", wt)
        self.write("old.sh", wt, age_days=30)
        self.write("notes.txt", wt)
        for d in ("node_modules", ".venv", "venv", ".git", "evidence"):
            self.write(f"{d}/x.sh", wt)
        self.write("bin.sh", b"\0\x01" + wt.encode())
        self.write("big.sh", wt + "#" * (1024 * 1024))
        # WHEN scanned with no window flag, with --since 7d, then 3650d, then with a .txt glob
        _, default, _ = self.scan(str(self.tree))
        _, week, s1 = self.scan(str(self.tree), "--since", "7d")
        _, all_time, _ = self.scan(str(self.tree), "--since", "3650d")
        _, txt, _ = self.scan(str(self.tree), "--glob", "*.txt")
        # THEN only new.sh by default and in the week, old.sh too over ten years, and notes.txt only under its glob
        names = lambda hits: sorted(Path(h["file"]).name for h in hits)  # noqa: E731
        self.assertEqual(names(default), ["new.sh"])
        self.assertEqual(names(week), ["new.sh"])
        self.assertEqual(s1["files_scanned"], 1)
        self.assertEqual(names(all_time), ["new.sh", "old.sh"])
        self.assertEqual(names(txt), ["notes.txt"])

    def test_from_until_scans_only_files_dated_inside_the_window(self):
        """--from/--until keep files whose mtime falls on a local date from FROM through UNTIL inclusive.

        The gardener replays a chosen past week, so the scan must be able to end before today; the last
        day counts until midnight, and a file one minute past it is out.
        """
        # GIVEN files dated the day before, the first minute of, the last minute of, and the day after the window
        self.at("before.sh", "2026-03-31", 23)
        self.at("first.sh", "2026-04-01", 0)
        self.at("last.sh", "2026-04-07", 23)
        self.at("after.sh", "2026-04-08", 0)
        # WHEN scanned with --from 2026-04-01 --until 2026-04-07, then with --from alone and --until alone
        r, both, summary = self.scan(str(self.tree), "--from", "2026-04-01", "--until", "2026-04-07")
        _, from_only, _ = self.scan(str(self.tree), "--from", "2026-04-01")
        _, until_only, _ = self.scan(str(self.tree), "--until", "2026-04-07")
        # THEN the window holds first and last; --from alone runs to now, --until alone from the start
        names = lambda hits: sorted(Path(h["file"]).name for h in hits)  # noqa: E731
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(names(both), ["first.sh", "last.sh"])
        self.assertEqual(summary["files_scanned"], 2)
        self.assertEqual(names(from_only), ["after.sh", "first.sh", "last.sh"])
        self.assertEqual(names(until_only), ["before.sh", "first.sh", "last.sh"])

    def test_from_until_refuse_since_a_bad_date_and_a_reversed_window(self):
        """--since with --from or --until, a date not YYYY-MM-DD, and FROM after UNTIL each exit 2 naming the fix.

        Two ways to say the window at once would make the scan guess which one the caller meant.
        """
        # GIVEN one file in the tree
        self.at("x.sh", "2026-04-02")
        cases = {("--since", "7d", "--from", "2026-04-01"): "--since",
                 ("--since", "7d", "--until", "2026-04-07"): "--since",
                 ("--from", "4/1/2026"): "YYYY-MM-DD",
                 ("--until", "9999-12-31"): "YYYY-MM-DD",
                 ("--from", "0001-01-01"): "YYYY-MM-DD",
                 ("--from", "2026-04-08", "--until", "2026-04-07"): "after"}
        for args, word in cases.items():
            # WHEN scan runs with the bad window
            r = agent_cli(self.p, "hook", "scan", str(self.tree), *args)
            # THEN exit 2, an error naming the problem, and an example using --from/--until
            self.assertEqual(r.returncode, 2, (args, r.stdout, r.stderr))
            self.assertIn(word, r.stderr)
            self.assertRegex(r.stderr, r"example: .*agent-cli hook scan .*--from")

    def test_help_lists_a_from_until_example(self):
        """`hook scan --help` shows a --from/--until example, so a caller finds the past-window form
        (cli-for-agents "`--help` that works")."""
        # GIVEN the installed CLI / WHEN asked for help
        r = agent_cli(self.p, "hook", "scan", "--help")
        # THEN the examples carry a dated window
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertRegex(r.stdout, r"agent-cli hook scan .*--from \d{4}-\d\d-\d\d --until \d{4}-\d\d-\d\d")

    def test_requires_is_ignored_and_project_rules_are_scanned(self):
        """A project rule from the seam is scanned too, even when its required command is not installed.

        Scan asks whether a file hand-rolls a chore; whether the replacement is installed here is beside it.
        """
        # GIVEN a project rule needing a missing command, and a file it matches
        rule = [{"id": "proj-deploy", "match": r"{CMD}\./deploy\.sh\b", "mode": "warn", "message": "use the CLI",
                 "requires": "no-such-command-xyz"}]
        make_project(self.p, seams={"pretooluse-rules.json": json.dumps(rule)})
        self.write("ship.sh", "set -e\n./deploy.sh prod\n")
        # WHEN scanned
        _, hits, _ = self.scan(str(self.tree))
        # THEN the project rule hits on line 2
        self.assertEqual([(h["rule"], h["line"]) for h in hits], [("proj-deploy", 2)])

    def test_no_paths_scans_the_gardener_scan_dirs_line(self):
        """With no paths, scan reads `scan-dirs:` under `## gardener` in project-rules.md, relative to the root.

        The gardener runs one command per week; the dirs to watch live with the project's other rules.
        """
        # GIVEN two dirs, only one listed in project-rules.md
        (self.p / "other").mkdir()
        (self.p / "other/wt.sh").write_text("git worktree add -b z ../z\n")
        self.write("wt.sh", "git worktree add -b x ../x\n")
        (self.p / ".claude/project-rules.md").write_text("## coordinator\nrequired-checks: test\n\n"
                                                          "## gardener\nscan-dirs: evals\n")
        # WHEN scanned with no paths
        r, hits, _ = self.scan()
        # THEN only the listed dir is scanned
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([Path(h["file"]).parent.name for h in hits], ["evals"])

    def test_usage_errors_exit_2_with_an_example(self):
        """No paths and no scan-dirs, a missing path, a bad --since, and --stdin with paths each exit 2
        (cli-for-agents "Fail fast with actionable errors")."""
        # GIVEN no project-rules.md
        cases = [(), ("missing-dir",), (str(self.tree), "--since", "soon"), ("--stdin", str(self.tree))]
        for args in cases:
            # WHEN scan runs with bad usage
            r = agent_cli(self.p, "hook", "scan", *args, stdin="")
            # THEN exit 2 with an error and an example
            self.assertEqual(r.returncode, 2, (args, r.stdout))
            self.assertIn("error:", r.stderr)
            self.assertRegex(r.stderr, r"example: .*agent-cli hook scan")

    def test_a_1mb_file_of_near_misses_scans_in_under_10s(self):
        """30,000 `while` lines and one `sleep 60` finish in under 10 s and still report the loop.

        Each `while` starts a bounded body search; over a whole file that is quadratic without windows.
        """
        # GIVEN a ~1 MB file of while lines ending in one long sleep
        self.write("big.py", "while x < 10:  x = x + 1  # pad\n" * 30000 + "sleep 60\n")
        # WHEN scanned
        start = time.monotonic()
        r, hits, _ = self.scan(str(self.tree))
        # THEN it is fast, and the hit is on the first `while` whose bounded (2,000-char) body reaches the
        # sleep: 2,000 chars of 27-char code lines (the comment is stripped) back from line 30,001
        self.assertLess(time.monotonic() - start, 10)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([(h["rule"], h["line"]) for h in hits], [("hand-sleep-poll", 30001 - 2000 // 27)])

    def test_a_1mb_file_of_assignments_scans_in_under_10s(self):
        """250,000 `X=1` lines then one worktree add finish in under 10 s.

        VAR= words used to be skipped across newlines, so each line rescanned every line after it.
        """
        # GIVEN a ~1 MB file of assignments ending in a worktree add
        self.write("assign.sh", "X=1\n" * 250000 + "git worktree add -b x ../x\n")
        # WHEN scanned
        start = time.monotonic()
        r, hits, _ = self.scan(str(self.tree))
        # THEN it is fast and the worktree add is found on its line
        self.assertLess(time.monotonic() - start, 10)
        self.assertEqual([(h["rule"], h["line"]) for h in hits], [("hand-worktree-add", 250001)])

    def test_a_stray_sleep_in_another_window_never_hides_or_fakes_a_hit(self):
        """A short stray `sleep` far from the real loop (before it, or after it) gives one hit on the loop.

        `needs` hits split the file into windows; a match must be found in whichever window holds it.
        """
        pad = "echo padding line\n" * 3000  # ~54 KB, wider than two windows
        loop = "until test -f x; do\n  sleep 30\ndone\n"
        for name, text, line in (("before.sh", "sleep 1\n" + pad + loop, 3002),
                                 ("after.sh", loop + pad + "sleep 1\n", 1)):
            with self.subTest(name):
                # GIVEN the stray sleep on one side of the padding and the loop on the other
                f = self.write(name, text)
                # WHEN scanned
                _, hits, _ = self.scan(str(f))
                # THEN exactly one hit, on the loop line
                self.assertEqual([(h["rule"], h["line"]) for h in hits], [("hand-sleep-poll", line)])

    def test_unless_applies_to_each_match_not_the_whole_file(self):
        """An `unless` hit elsewhere in the file hides only its own command, never a real hit beside it.

        A `--detach` add, or a PR poll loop, in the same script must not erase a plain add or sleep loop.
        """
        cases = {
            "a.sh": ("#!/bin/bash\ngit worktree add ../wt feature-x\necho done\n"
                     "git worktree add --detach ../wt2 HEAD\n", [("hand-worktree-add-other", 2)]),
            "a2.sh": "#!/bin/bash\ngit worktree add ../wt feature-x\necho done\n",
            "b.sh": ("#!/bin/bash\nuntil curl -sf localhost:3000; do sleep 30; done\necho next\n"
                     "while true; do gh pr checks 12 && break; sleep 30; done\n",
                     [("hand-pr-poll", 4), ("hand-sleep-poll", 2)]),
        }
        cases["a2.sh"] = (cases["a2.sh"], [("hand-worktree-add-other", 2)])
        for name, (text, want) in cases.items():
            with self.subTest(name):
                # GIVEN a script mixing a real hit with an unless-exempt command
                f = self.write(name, text)
                # WHEN scanned
                _, hits, _ = self.scan(str(f))
                # THEN the real hit is reported on its line
                self.assertEqual([(h["rule"], h["line"]) for h in hits], want)

    def test_unreadable_file_is_skipped_and_counted(self):
        """A file that cannot be read is counted as unreadable; the scan still runs and exits 0."""
        if os.geteuid() == 0:
            self.skipTest("root reads any file")
        # GIVEN one readable hit and one unreadable file
        self.write("ok.sh", "git worktree add -b x ../x\n")
        locked = self.write("locked.sh", "git worktree add -b y ../y\n")
        locked.chmod(0)
        self.addCleanup(locked.chmod, 0o644)
        # WHEN scanned
        r, hits, summary = self.scan(str(self.tree))
        # THEN exit 0, the readable hit only, and one unreadable
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([Path(h["file"]).name for h in hits], ["ok.sh"])
        self.assertEqual((summary["files_scanned"], summary["unreadable"]), (1, 1))


class Stdin(ScanCase):
    def test_stdin_scans_one_command_per_line_with_bypass(self):
        """--stdin treats each line as a command: hits carry the line number, file "-" and the bypass flag.

        Evals pipe a session's commands through this to list the hand-rolled ones.
        """
        # GIVEN three commands: clean, a PR poll loop, and a bypassed worktree add
        cmds = ("ls -la\n\nwhile true; do gh pr checks 3; sleep 30; done\n"
                "git worktree add -b x ../x  # agent-cli: allow hand-worktree-add\n")
        # WHEN piped to scan --stdin
        r, hits, summary = self.scan("--stdin", stdin=cmds)
        # THEN two hits on lines 3 and 4, the second bypassed
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([(h["file"], h["line"], h["rule"], h["bypassed"]) for h in hits],
                         [("-", 3, "hand-pr-poll", False), ("-", 4, "hand-worktree-add", True)])
        self.assertEqual(summary, {"commands_scanned": 3, "bypassed": 1,
                                   "hits_by_rule": {"hand-pr-poll": 1, "hand-worktree-add": 1}})

    def test_plain_output_is_one_line_per_hit_then_the_summary(self):
        """Without --json each hit is `file:line  rule (mode)` and the summary is key: value lines."""
        # GIVEN one bypassed command
        # WHEN scanned without --json
        r = agent_cli(self.p, "hook", "scan", "--stdin",
                      stdin="git worktree add -b x ../x  # agent-cli: allow hand-worktree-add\n")
        # THEN the hit line and the summary lines
        self.assertEqual(r.stdout.splitlines(), ["-:1  hand-worktree-add (deny)  bypassed", "commands_scanned: 1",
                                                 'hits_by_rule: {"hand-worktree-add": 1}', "bypassed: 1"])


if __name__ == "__main__":
    unittest.main()
