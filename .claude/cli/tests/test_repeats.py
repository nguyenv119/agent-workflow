"""Tests for `agent-cli repeats`: shapes typed in 3+ sessions, minus the ones a command or hook rule covers."""
import datetime as dt
import json
import sys
import time
import unittest
from pathlib import Path

from helpers import CLAUDE, TmpCase, agent_cli, make_project, set_env, store_rows
from helpers import post_event

sys.path.insert(0, str(CLAUDE / "cli"))
from agent_cli import record, repeats  # noqa: E402

UTC = dt.timezone.utc
DAY = dt.datetime(2026, 4, 2, 12, 0, 0, tzinfo=UTC)
POLL = "while true; do gh pr checks 12 && break; sleep 30; done"  # the built-in hand-pr-poll chore
POLL_STEPS = ["gh pr checks <n>", "sleep <n>"]  # its listed steps (the sleep is report-only)
SEAMS = {"command-prefixes": "wrapx\nwrapx raw\n"}  # a made-up wrapper word, never a real one
UTC_ENV = {"TZ": "UTC"}  # --from/--until are local times; pin the zone so the bounds are exact


class StoreCase(TmpCase):
    """A temp project with agent-cli installed, and a shape store in a temp dir filled by record.record."""

    def setUp(self):
        super().setUp()
        self.p = make_project(self.tmp / "proj", seams=SEAMS)
        self.store = self.tmp / "shapes"
        set_env(self, AGENT_CLI_SHAPES_DIR=str(self.store))

    def rec(self, command, sessions, at=DAY, **extra):
        """Record command once per session in sessions (an int n means sessions s0..s<n-1>) at time at."""
        for s in (range(sessions) if isinstance(sessions, int) else sessions):
            extra.setdefault("cwd", None)  # no cwd: repo null, and record() runs no git
            record.record(post_event(command, session=f"s{s}" if isinstance(s, int) else s, **extra), now=at)

    def repeats(self, *args, env=None):
        """Run `agent-cli repeats ... --json`; returns (CompletedProcess, rows)."""
        r = agent_cli(self.p, "repeats", *args, "--json",
                      env=dict({"AGENT_CLI_SHAPES_DIR": str(self.store)}, **(env or {})))
        return r, [json.loads(ln) for ln in r.stdout.splitlines()]

    def shapes(self, *args, env=None):
        r, rows = self.repeats(*args, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        return [row["shape"] for row in rows]

    def write_commands(self, rows):
        """A --commands JSONL file of {ts, session, cmd} rows."""
        f = self.tmp / "in" / "commands.jsonl"
        f.parent.mkdir(exist_ok=True)
        f.write_text("".join(json.dumps(r) + "\n" for r in rows))
        return f


class Threshold(StoreCase):
    def test_a_shape_in_3_sessions_is_listed_and_one_in_2_is_not(self):
        """The report lists a shape only when 3+ distinct sessions typed it (the default --min-sessions).

        Many runs inside one session are one agent's loop, not a chore that keeps coming back; counting
        sessions, not runs, is what separates a repeated chore from a busy afternoon.
        """
        # GIVEN one shape in 3 sessions, another in 2 sessions typed 5 times each
        self.rec("lint-tool -q", 3)
        for _ in range(5):
            self.rec("uptime", 2)
        # WHEN the report runs over the window holding them
        r, rows = self.repeats("--from", "2026-04-02", "--until", "2026-04-02", env=UTC_ENV)
        # THEN only the 3-session shape is listed, with its counts and times
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(rows, [{"shape": "lint-tool -q", "sessions": 3, "count": 3, "first": "2026-04-02T12:00:00Z",
                                 "last": "2026-04-02T12:00:00Z", "exit_fail_share": 0.0}])

    def test_min_sessions_flag_lowers_the_bar(self):
        """--min-sessions 2 lists the 2-session shape too.

        Test runs and small repos need a lower bar; the flag must change the threshold, not be ignored.
        """
        # GIVEN a shape in 2 sessions
        self.rec("make docs", 2)
        # WHEN the report runs with --min-sessions 2
        # THEN it is listed
        self.assertEqual(self.shapes("--since", "3650d", "--min-sessions", "2"), ["make <arg>"])

    def test_rank_is_sessions_then_count_and_top_cuts_the_list(self):
        """Shapes rank by sessions, then by count; --top keeps the first N.

        Test A reads the top 25, so a shape seen in more sessions must outrank one typed more often in
        fewer sessions, and ties must break the same way every run.
        """
        # GIVEN shapes in 4 sessions (once each), 3 sessions (twice each) and 3 sessions (once each)
        self.rec("lint-tool -q", 4)
        self.rec("gh pr list", 3)
        self.rec("gh pr list", 3)
        self.rec("uptime", 3)
        # WHEN the report runs, then again with --top 2
        # THEN the order is by sessions, then count, and --top 2 keeps the first two
        self.assertEqual(self.shapes("--since", "3650d"), ["lint-tool -q", "gh pr list", "uptime"])
        self.assertEqual(self.shapes("--since", "3650d", "--top", "2"), ["lint-tool -q", "gh pr list"])

    def test_exit_fail_share_is_the_share_of_failed_runs(self):
        """exit_fail_share is failed runs over runs with a known exit code.

        A chore that often fails is a better command candidate than one that never does; a wrong share
        would rank them the other way.
        """
        # GIVEN a shape in 3 sessions, one of its runs failed
        self.rec("make lint", 2)
        record.record(post_event("make lint", name="PostToolUseFailure", session="s2", cwd=None,
                                 error="Exit code 2"), now=DAY)
        # WHEN the report runs
        r, rows = self.repeats("--since", "3650d")
        # THEN a third of the runs failed
        self.assertEqual([(x["sessions"], x["count"], round(x["exit_fail_share"], 3)) for x in rows], [(3, 3, 0.333)])

    def test_a_row_with_no_session_counts_as_a_run_not_a_session(self):
        """A row with session null adds to count but not to sessions.

        Sessions is the bar for a repeat; counting every unnamed row as a new session would push a
        one-off over --min-sessions.
        """
        # GIVEN a shape in 2 named sessions plus 2 rows with no session
        self.rec("lint-tool -q", [0, 1, None, None])
        # WHEN the report runs with --min-sessions 2, and with the default 3
        r, got = self.repeats("--since", "3650d", "--min-sessions", "2")
        # THEN it has 2 sessions and 4 runs, and the default bar drops it
        self.assertEqual([(x["sessions"], x["count"]) for x in got], [(2, 4)], r.stderr)
        self.assertEqual(self.shapes("--since", "3650d"), [])

    def test_repo_flag_keeps_one_repos_runs(self):
        """--repo counts only the rows recorded in that repo.

        One store holds every repo; without the filter, 2 sessions in one repo and 2 in another would add
        up to a 4-session repeat that neither repo has.
        """
        # GIVEN a shape in 2 sessions of repo-a and 2 of repo-b
        a = make_project(self.tmp / "repo-a", install=False)
        b = make_project(self.tmp / "repo-b", install=False)
        self.rec("make lint", ["a1", "a2"], cwd=str(a))
        self.rec("make lint", ["b1", "b2"], cwd=str(b))
        # WHEN the report runs for repo-a with --min-sessions 2, and for all repos with the default 3
        # THEN repo-a alone has 2 sessions, and across repos it is 4
        _, rows = self.repeats("--since", "3650d", "--repo", "repo-a", "--min-sessions", "2")
        self.assertEqual([x["sessions"] for x in rows], [2])
        _, rows = self.repeats("--since", "3650d")
        self.assertEqual([x["sessions"] for x in rows], [4])


class Covered(StoreCase):
    def test_builtin_rule_shape_is_dropped_live_and_listed_with_none(self):
        """A shape whose rows carry a built-in rule id (hand-pr-poll) is covered: dropped with
        --covered live (the default), listed with --covered none.

        A chore a rule already points at a command needs no new command; --covered none exists to replay
        a window from before the commands existed, when the same chore was a real gap.
        """
        # GIVEN the hand-written PR poll in 3 sessions (its rows name hand-pr-poll)
        self.rec(POLL, 3)
        # WHEN the report runs with the live filter and with none
        live = self.shapes("--since", "3650d")
        none = self.shapes("--since", "3650d", "--covered", "none")
        # THEN it is dropped live and listed with none
        self.assertEqual(live, [])
        self.assertEqual(none, POLL_STEPS)

    def test_a_shape_is_covered_only_when_half_its_runs_matched_a_rule(self):
        """A rule match on 1 of 4 runs leaves the shape listed; on 2 of 4 runs it is covered.

        A generic shape (`python3 - <heredoc>`) masks the body, so it holds many different chores. One run
        whose body a rule caught must not hide the other runs, which no rule or command covers.
        """
        # GIVEN two `<tool> -c <str>` shapes in 4 sessions: one rule-matched run in the first, two in the second
        rules = json.dumps([{"id": "demo-rule", "mode": "warn", "on": "raw", "match": "DEMO_TOKEN",
                             "message": "use the demo command"}])
        (self.p / ".claude/agent-cli-hooks/pretooluse-rules.json").write_text(rules)
        rows = [("psql -c 'select DEMO_TOKEN'" if i < 1 else "psql -c 'select 1'", i) for i in range(4)]
        rows += [("mysql -c 'select DEMO_TOKEN'" if i < 2 else "mysql -c 'select 1'", i) for i in range(4)]
        cmds = self.write_commands([{"ts": "2026-04-02T12:00:00Z", "session": f"s{i}", "cmd": c} for c, i in rows])
        # WHEN the report runs live
        # THEN the 1-in-4 shape is listed and the 2-in-4 shape is covered
        self.assertEqual(self.shapes("--commands", str(cmds)), ["psql -c <str>"])

    def test_a_bypassed_rule_still_counts_as_covered(self):
        """A shape whose only rows bypassed a rule (`# agent-cli: allow <id>`) is covered.

        A deny rule blocks the command, so the recorder only ever sees a denied chore when an agent
        bypassed the rule on purpose. Those rows are the chore the rule's command already covers;
        listing them would ask for a command that exists.
        """
        # GIVEN the PR poll typed with a bypass comment in 3 sessions
        self.rec(POLL + "  # agent-cli: allow hand-pr-poll", 3)
        # WHEN the report runs live, and with none
        # THEN dropped live, listed with none
        self.assertEqual(self.shapes("--since", "3650d"), [])
        self.assertEqual(self.shapes("--since", "3650d", "--covered", "none"), POLL_STEPS)

    def test_a_bypassed_rule_in_commands_input_counts_as_covered(self):
        """From --commands, a raw command that bypassed a rule still counts as covered.

        Transcripts hold a deny-rule chore only when it was bypassed or denied; the replay must judge it
        the way the store does, or the same chore would be covered in one source and not the other.
        """
        # GIVEN the PR poll with a bypass comment as raw replay input in 3 sessions
        cmds = self.write_commands([{"ts": "2026-04-02T12:00:00Z", "session": f"s{i}",
                                     "cmd": POLL + "  # agent-cli: allow hand-pr-poll"} for i in range(3)])
        # WHEN the report runs live and with none
        # THEN dropped live, listed with none
        self.assertEqual(self.shapes("--commands", str(cmds)), [])
        self.assertEqual(self.shapes("--commands", str(cmds), "--covered", "none"), POLL_STEPS)

    def test_agent_cli_behind_a_seam_wrapper_is_covered(self):
        """A shape that starts `<wrapper> agent-cli ...` is covered once the wrapper is in the
        command-prefixes seam, both from the store and from --commands.

        Recorders that ran without the seam keep the wrapper as the command word; the report strips it
        with the seam it reads now, so `agent-cli` is still seen as the command.
        """
        # GIVEN `wrapx agent-cli wt list` recorded in 3 sessions through this project's hook verb
        for s in range(3):
            agent_cli(self.p, "hook", "posttooluse", stdin=json.dumps(post_event("wrapx agent-cli wt list", session=f"s{s}", cwd=None)),
                      env={"AGENT_CLI_SHAPES_DIR": str(self.store)})
        stored = [r["shape"] for r in store_rows(self.store)]
        # and the same command as raw --commands input
        cmds = self.write_commands([{"ts": "2026-04-02T12:00:00Z", "session": f"s{i}",
                                     "cmd": "wrapx agent-cli wt new demo-1 fix"} for i in range(3)])
        # WHEN the report runs over each, live and with none
        # THEN the store shapes were stripped at record time (the seam is installed), and both are covered live
        self.assertEqual(set(stored), {"agent-cli wt list"})
        self.assertEqual(self.shapes("--since", "3650d"), [])
        self.assertEqual(self.shapes("--commands", str(cmds)), [])
        self.assertEqual(self.shapes("--commands", str(cmds), "--covered", "none"), ["agent-cli wt new <arg>"])

    def test_wrapper_left_in_a_stored_shape_is_stripped_at_report_time(self):
        """A stored shape that still starts with a wrapper word (`wrapx agent-cli <arg>`) is covered when
        the seam lists the wrapper now.

        Rows from a recorder that had no wrapper seam keep the wrapper; the report must apply today's
        seam to the stored shape, or every such row would look uncovered.
        """
        # GIVEN rows shaped by a project with no wrapper seam but with wrapx as a multi-command tool
        bare = make_project(self.tmp / "bare", seams={"subcommand-tools": "wrapx\n"})
        for s in range(3):
            agent_cli(bare, "hook", "posttooluse", stdin=json.dumps(post_event("wrapx agent-cli wt list", session=f"s{s}", cwd=None)),
                      env={"AGENT_CLI_SHAPES_DIR": str(self.store)})
        # WHEN this project (whose seam lists wrapx) reports on them, live and with none
        # THEN the stored shape keeps the wrapper, and it is covered live
        self.assertEqual(self.shapes("--since", "3650d", "--covered", "none"), ["wrapx agent-cli <arg>"])
        self.assertEqual(self.shapes("--since", "3650d"), [])

    def test_commands_line_under_repeats_covers_a_project_cli(self):
        """A shape whose command is on the `commands:` line under `## repeats` in project-rules.md is covered.

        A project's own CLI is a command the chore already has; without this line its calls would top
        the report as uncovered chores.
        """
        # GIVEN a made-up project CLI in 3 sessions, and a project-rules.md naming it
        self.rec("tasker-tool sync", 3)
        (self.p / ".claude/project-rules.md").write_text("# rules\n\n## repeats\ncommands: `tasker-tool`, other-cli\n")
        # WHEN the report runs live and with none
        # THEN dropped live, listed with none
        self.assertEqual(self.shapes("--since", "3650d"), [])
        self.assertEqual(self.shapes("--since", "3650d", "--covered", "none"), ["tasker-tool <arg>"])

    def test_live_rule_match_covers_a_commands_input_shape(self):
        """From --commands, a raw command that a rule matches now is covered (the rule ids are computed
        in memory, as the recorder does).

        The replay test judges the filter on old commands; the rules that exist today must decide.
        """
        # GIVEN the PR poll as raw replay input in 3 sessions
        cmds = self.write_commands([{"ts": "2026-04-02T12:00:00Z", "session": f"s{i}", "cmd": POLL} for i in range(3)])
        # WHEN the report runs live and with none
        # THEN dropped live, listed with none
        self.assertEqual(self.shapes("--commands", str(cmds)), [])
        self.assertEqual(self.shapes("--commands", str(cmds), "--covered", "none"), POLL_STEPS)

    def test_live_warn_rule_covers_a_one_shot_pr_read(self):
        """From --commands, a one-shot `gh pr view <n> --json state` (the hand-pr-status warn) is covered.

        The warn already points at agent-cli pr status, so the report must not ask for a new command.
        """
        # GIVEN the one-shot PR read as raw replay input in 3 sessions
        cmds = self.write_commands([{"ts": "2026-04-02T12:00:00Z", "session": f"s{i}",
                                     "cmd": "gh pr view 12 --json state"} for i in range(3)])
        # WHEN the report runs live and with none
        # THEN dropped live, listed with none
        self.assertEqual(self.shapes("--commands", str(cmds)), [])
        self.assertEqual(len(self.shapes("--commands", str(cmds), "--covered", "none")), 1)


class Window(StoreCase):
    def test_from_is_inclusive_and_until_with_a_time_is_exclusive(self):
        """--from T keeps a row at exactly T; --until T drops a row at exactly T (local time).

        Test A cuts the pre-CLI window at a minute; a row at that minute belongs to the next window, and
        an off-by-one would put one chore in both.
        """
        # GIVEN one shape per minute boundary: at 10:00, at 11:00, and at 10:59:59, each in 3 sessions
        self.rec("make start", 3, at=dt.datetime(2026, 4, 2, 10, 0, 0, tzinfo=UTC))
        self.rec("lint-tool -q", 3, at=dt.datetime(2026, 4, 2, 10, 59, 59, tzinfo=UTC))
        self.rec("uptime", 3, at=dt.datetime(2026, 4, 2, 11, 0, 0, tzinfo=UTC))
        # WHEN the window is 10:00 to 11:00
        got = self.shapes("--from", "2026-04-02T10:00", "--until", "2026-04-02T11:00", env=UTC_ENV)
        # THEN the 10:00 and 10:59:59 shapes are in, the 11:00 one is out
        self.assertEqual(sorted(got), ["lint-tool -q", "make <arg>"])

    def test_from_and_until_are_local_times(self):
        """--from/--until are read in the local time zone; rows are stored in UTC.

        At UTC+7, 10:00 local is 03:00Z. Reading the flags as UTC would shift every window by 7 hours, and
        a pre-CLI window would take in a morning of post-CLI runs.
        """
        # GIVEN rows at 03:00Z (10:00 at UTC+7) and at 02:59:59Z, each in 3 sessions
        self.rec("lint-tool -q", 3, at=dt.datetime(2026, 4, 2, 3, 0, 0, tzinfo=UTC))
        self.rec("uptime", 3, at=dt.datetime(2026, 4, 2, 2, 59, 59, tzinfo=UTC))
        # WHEN the window starts, or ends, at 10:00 in a UTC+7 zone (no DST there)
        zone = {"TZ": "Asia/Ho_Chi_Minh"}
        after = self.shapes("--from", "2026-04-02T10:00", env=zone)
        before = self.shapes("--until", "2026-04-02T10:00", env=zone)
        # THEN 03:00Z is the first second inside --from and outside --until
        self.assertEqual((after, before), (["lint-tool -q"], ["uptime"]))

    def test_commands_input_uses_the_same_bounds(self):
        """Over --commands, a row at exactly --until is out and one at exactly --from is in; offsets are
        converted to UTC first.

        The replay windows of Test A are cut on --commands input, so its bounds must match the store's.
        """
        # GIVEN rows at 10:00Z, 10:59:59Z, 10:30Z written as 12:30+02:00, 11:00Z written as 13:00+02:00,
        # and 11:00Z, 3 sessions each (12:30+02:00 is in only once converted to UTC)
        rows = []
        for i in range(3):
            rows += [{"ts": "2026-04-02T10:00:00Z", "session": f"s{i}", "cmd": "make start"},
                     {"ts": "2026-04-02T10:59:59.500Z", "session": f"s{i}", "cmd": "lint-tool -q"},
                     {"ts": "2026-04-02T13:00:00+02:00", "session": f"s{i}", "cmd": "uptime"},
                     {"ts": "2026-04-02T12:30:00+02:00", "session": f"s{i}", "cmd": "whoami"},
                     {"ts": "2026-04-02T11:00:00Z", "session": f"s{i}", "cmd": "hostname"}]
        cmds = self.write_commands(rows)
        # WHEN the window is 10:00 to 11:00 UTC
        got = self.shapes("--commands", str(cmds), "--from", "2026-04-02T10:00", "--until", "2026-04-02T11:00",
                          env=UTC_ENV)
        # THEN 10:00, 10:59:59.5 and 12:30+02:00 are in; both spellings of 11:00 are out
        self.assertEqual(sorted(got), ["lint-tool -q", "make <arg>", "whoami"])

    def test_until_with_a_bare_date_includes_that_whole_day(self):
        """--until YYYY-MM-DD keeps the whole day; the next midnight is out.

        Matches `hook scan --until`, so the two commands read a date the same way.
        """
        # GIVEN shapes at 23:59:59 on 2 Apr and at 00:00 on 3 Apr
        self.rec("lint-tool -q", 3, at=dt.datetime(2026, 4, 2, 23, 59, 59, tzinfo=UTC))
        self.rec("uptime", 3, at=dt.datetime(2026, 4, 3, 0, 0, 0, tzinfo=UTC))
        # WHEN --until 2026-04-02
        # THEN only the 2 Apr shape
        self.assertEqual(self.shapes("--from", "2026-04-01", "--until", "2026-04-02", env=UTC_ENV), ["lint-tool -q"])

    def test_since_counts_back_from_now(self):
        """--since 14d keeps rows from the last 14 days and drops older ones.

        The warning hook's cache is written with --since 14d at session start; old chores that stopped
        must age out.
        """
        # GIVEN a shape 2 days old and one 20 days old
        now = dt.datetime.now(UTC)
        self.rec("lint-tool -q", 3, at=now - dt.timedelta(days=2))
        self.rec("uptime", 3, at=now - dt.timedelta(days=20))
        # WHEN --since 14d (and the default, which is the same)
        # THEN only the recent shape
        self.assertEqual(self.shapes("--since", "14d"), ["lint-tool -q"])
        self.assertEqual(self.shapes(), ["lint-tool -q"])

    def test_commands_input_counts_every_row_without_a_window(self):
        """With --commands and no window flag, every input row counts (no 14-day default).

        Replay files hold weeks-old commands; a now-relative default would silently drop all of them.
        """
        # GIVEN replay input from long ago, with fractional-second timestamps
        cmds = self.write_commands([{"ts": "2020-01-0%dT10:00:00.123Z" % (i + 1), "session": f"s{i}", "cmd": "make lint"}
                                    for i in range(3)])
        # WHEN the report runs with no window
        r, rows = self.repeats("--commands", str(cmds))
        # THEN the shape is listed with its first and last times, and no fail share (the input has no exit)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(rows, [{"shape": "make <arg>", "sessions": 3, "count": 3, "first": "2020-01-01T10:00:00Z",
                                 "last": "2020-01-03T10:00:00Z", "exit_fail_share": None}])


class Privacy(StoreCase):
    def test_commands_input_text_is_written_nowhere(self):
        """Running over --commands writes no file holding any piece of the raw commands.

        The replay input is raw transcript text with whatever secrets it carried; the report may only
        ever hold shapes. A byte search of every file under the temp dir (the input file aside) and of
        the output finds no 6-char piece of the planted secret.
        """
        # GIVEN raw commands carrying a planted secret in 3 sessions, the store and the project in the temp dir
        secret = "sk-live-PLANTED0123456789"
        cmds = self.write_commands([{"ts": "2026-04-02T12:00:00Z", "session": f"s{i}",
                                     "cmd": f"curl -H 'Authorization: {secret}' https://h.example.com/{secret}"}
                                    for i in range(3)])
        # WHEN the report runs over it, in text and JSON mode, and once with a bad line after it
        outs = []
        for extra in ((), ("--json",)):
            r = agent_cli(self.p, "repeats", "--commands", str(cmds), *extra, cwd=self.tmp,
                          env={"AGENT_CLI_SHAPES_DIR": str(self.store)})
            outs += [r.stdout, r.stderr]
            self.assertEqual(r.returncode, 0, r.stderr)
        with cmds.open("a") as f:
            f.write(f"not json {secret}\n")
        r = agent_cli(self.p, "repeats", "--commands", str(cmds), cwd=self.tmp, env={"AGENT_CLI_SHAPES_DIR": str(self.store)})
        outs += [r.stdout, r.stderr]
        # THEN the shape was listed, and no piece of the secret is in any output or any file but the input
        self.assertIn("curl -H <str> <url:h.example.com>", outs[0])
        pieces = {secret[i:i + 6].encode() for i in range(len(secret) - 5)}
        files = [f for f in self.tmp.rglob("*") if f.is_file() and f != cmds]
        hits = [str(f) for f in files if any(p in f.read_bytes() for p in pieces)]
        hits += [f"output {i}" for i, o in enumerate(outs) if any(p.decode() in o for p in pieces)]
        self.assertEqual(hits, [])


class Cache(StoreCase):
    def test_write_cache_holds_every_listed_shape_before_top(self):
        """--write-cache writes <store>/repeats-cache.json {shape: {sessions, count}} for every shape the
        report lists, ignoring --top.

        The warning hook looks a typed shape up in this file; a shape cut by --top would never warn.
        """
        # GIVEN two uncovered repeated shapes and one covered one
        self.rec("lint-tool -q", 4)
        self.rec("uptime", 3)
        self.rec(POLL, 3)
        # WHEN the report runs with --top 1 --write-cache
        r, rows = self.repeats("--since", "3650d", "--top", "1", "--write-cache")
        # THEN stdout has one row, and the cache has both uncovered shapes
        self.assertEqual((r.returncode, len(rows)), (0, 1), r.stderr)
        cache = json.loads((self.store / "repeats-cache.json").read_text())
        self.assertEqual(cache, {"lint-tool -q": {"sessions": 4, "count": 4}, "uptime": {"sessions": 3, "count": 3}})

    def test_a_cache_that_cannot_be_written_exits_3(self):
        """--write-cache into a store dir that refuses writes exits 3 (blocked), not a traceback.

        The gardener runs this unattended; a crash would hide why the warning hook's cache went stale.
        """
        # GIVEN a store dir the report cannot write into
        self.rec("lint-tool -q", 3)
        self.store.chmod(0o500)
        self.addCleanup(self.store.chmod, 0o700)
        # WHEN the report runs with --write-cache
        r, _ = self.repeats("--since", "3650d", "--write-cache")
        # THEN it exits 3 and no cache exists
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertFalse((self.store / "repeats-cache.json").exists())

    def test_without_write_cache_nothing_is_written(self):
        """The report is read-only unless --write-cache is given.

        It runs over the live store that sessions append to; a report that wrote there by default could
        race the recorder or leave a stale cache the warning hook trusts.
        """
        # GIVEN a store
        self.rec("lint-tool -q", 3)
        before = sorted(p.name for p in self.store.iterdir())
        # WHEN the report runs
        self.shapes("--since", "3650d")
        # THEN the store holds the same files
        self.assertEqual(sorted(p.name for p in self.store.iterdir()), before)


class Usage(StoreCase):
    def test_missing_store_exits_3_with_the_fix(self):
        """No store dir exits 3 (blocked) and says how to get one.

        An agent must tell "nothing repeated" (exit 0, empty) from "nothing was ever recorded" (exit 3).
        """
        # GIVEN no store dir
        # WHEN the report runs
        r, rows = self.repeats()
        # THEN exit 3, no rows, an error naming the recorder and an example
        self.assertEqual((r.returncode, rows), (3, []))
        self.assertIn("error:", r.stderr)
        self.assertIn("hook posttooluse", r.stderr)
        self.assertIn("example:", r.stderr)

    def test_a_cut_short_store_line_is_skipped_and_counted(self):
        """A store line that is not a whole JSON row (a crash mid-write) is skipped, counted on stderr, and
        the report still runs.

        The store is appended by many sessions; one broken line must not stop every report after it.
        """
        # GIVEN a shape in 3 sessions and a half-written line in the same day file
        self.rec("lint-tool -q", 3)
        with next(self.store.glob("*.jsonl")).open("a") as f:
            f.write('{"ts": "2026-04-02T12:00:00Z", "sess\n')
        # WHEN the report runs
        r, rows = self.repeats("--since", "3650d")
        # THEN exit 0, the shape listed, the skip counted
        self.assertEqual((r.returncode, [x["shape"] for x in rows]), (0, ["lint-tool -q"]), r.stderr)
        self.assertIn("1 unreadable store lines skipped", r.stderr)

    def test_a_store_row_with_bad_shape_or_steps_is_skipped_and_counted(self):
        """A store row whose steps is not a list of strings, or whose shape is not a string, is skipped as
        an unreadable line and counted on stderr; the other rows are still reported.

        A hand-edited or foreign row must not crash the report the warning cache is built from, nor be
        counted under a junk key.
        """
        # GIVEN a chore in 3 sessions and two bad rows in the same day file
        self.rec("lint-tool -q", 3)
        for bad in ({"steps": [1]}, {"shape": 7}):
            record.append_line(self.store / "2026-04-02.jsonl", json.dumps(
                dict({"ts": "2026-04-02T12:00:00Z", "session": "s9", "shape": "x-tool <arg>", "rules": []}, **bad)))
        # WHEN the report runs
        r, rows = self.repeats("--since", "3650d")
        # THEN exit 0, the chore listed, both rows counted as skipped
        self.assertEqual((r.returncode, [x["shape"] for x in rows]), (0, ["lint-tool -q"]), r.stderr)
        self.assertIn("2 unreadable store lines skipped", r.stderr)

    def test_summary_names_steps_or_shapes_by_unit(self):
        """The stderr summary counts "steps" with --unit step and "shapes" with --unit command.

        An agent reading the line must know which unit the counts are in; "shapes" over steps misleads it.
        """
        # GIVEN a chore in 3 sessions
        self.rec("lint-tool -q", 3)
        # WHEN the report runs in each unit
        step, _ = self.repeats("--since", "3650d")
        whole, _ = self.repeats("--since", "3650d", "--unit", "command")
        # THEN each summary names its unit
        self.assertIn("1 steps in 3+ sessions", step.stderr)
        self.assertIn("1 shapes in 3+ sessions", whole.stderr)

    def test_unreadable_or_bad_commands_input_exits_3(self):
        """A missing --commands file, or a line that is not a {ts, session, cmd} object, exits 3 and
        names the line.

        A replay over a broken extract must stop, not report a partial list as if it were the window.
        """
        # GIVEN a missing file and a file whose second line lacks session
        bad = self.write_commands([{"ts": "2026-04-02T12:00:00Z", "session": "a", "cmd": "ls"},
                                   {"ts": "2026-04-02T12:00:00Z", "cmd": "ls"}])
        # WHEN the report runs on each
        missing, _ = self.repeats("--commands", str(self.tmp / "nope.jsonl"))
        broken, _ = self.repeats("--commands", str(bad))
        # THEN both exit 3; the bad one names line 2
        self.assertEqual((missing.returncode, broken.returncode), (3, 3))
        self.assertIn("line 2", broken.stderr)

    def test_usage_errors_exit_2_with_an_example(self):
        """--since with --from, a bad date, --from after --until, --write-cache with --commands, --covered
        none, --repo or --unit command, an unknown --unit, and a zero --min-sessions or --top each exit 2
        with an example.

        Each is a caller mistake that a correct invocation fixes; exit 2 tells the agent to fix the call.
        """
        self.store.mkdir()
        cmds = self.write_commands([])
        for args in (("--since", "7d", "--from", "2026-04-01"), ("--from", "April"), ("--until", "2026-04-02T25:00"),
                     ("--from", "2026-04-03", "--until", "2026-04-02"), ("--commands", str(cmds), "--write-cache"),
                     ("--write-cache", "--covered", "none"), ("--write-cache", "--repo", "x"),
                     ("--write-cache", "--unit", "command"), ("--unit", "word"),
                     ("--min-sessions", "0"), ("--top", "0"), ("--covered", "some")):
            with self.subTest(args=args):
                # GIVEN a bad combination
                # WHEN the report runs
                r, _ = self.repeats(*args)
                # THEN exit 2 with an example
                self.assertEqual(r.returncode, 2, r.stderr)
                self.assertIn("example:", r.stderr)

    def test_help_has_examples_and_the_exit_codes(self):
        """`repeats --help` shows an Examples block, the exit codes it uses, and unit-neutral flag help.

        house-rules: every command documents its exit codes against the CLI's one table, and help that
        works has runnable examples.
        """
        # WHEN help is asked for
        r = agent_cli(self.p, "repeats", "--help")
        # THEN examples and each exit code with its meaning
        self.assertEqual(r.returncode, 0)
        self.assertIn("Examples:\n  agent-cli repeats", r.stdout)
        for line in ("0 ok", "2 refused", "3 blocked"):
            self.assertIn(line, r.stdout)
        for text in ("sessions a row needs", "first N rows", "object per row"):  # rows: steps, shapes or effects
            self.assertIn(text, r.stdout)

    def test_text_mode_prints_a_table_with_the_same_fields(self):
        """Without --json the report is a table whose header names every JSON field.

        house-rules: text output carries the same fields as --json.
        """
        # GIVEN a listed shape
        self.rec("lint-tool -q", 3)
        # WHEN the report runs in text mode
        r = agent_cli(self.p, "repeats", "--since", "3650d", env={"AGENT_CLI_SHAPES_DIR": str(self.store)})
        # THEN a header with every field, then the row
        head, row = r.stdout.splitlines()
        self.assertEqual(head.split(), ["sessions", "count", "exit_fail_share", "first", "last", "shape"])
        self.assertEqual(row.split()[:3] + row.split()[5:], ["3", "3", "0.00", "lint-tool", "-q"])


ENV_CHAINS = ["export PATH=/opt/bin:$PATH && set -a && . .env && set +a && mkdir -p out && cp a.txt out/ && cat out/a.txt",
              "set -a; . .env; set +a; git add a.txt && git commit -m 'msg'",
              "mkdir -p /tmp/x && . .env && export FOO=bar && cat /tmp/x/y | grep -n key",
              ". .env && cp a b && git add . && git commit -m 'wip' && git push"]
# sleep 5: under the 20 s that the built-in hand-bare-sleep rule warns on, so no rule covers the row
CHORES = "set -a; . .env; sleep 5; git log origin/main.. --oneline; git log HEAD..origin/main; deploy-tool push; cat a.txt"


class Steps(StoreCase):
    def rows(self, *args):
        r, rows = self.repeats(*args)
        self.assertEqual(r.returncode, 0, r.stderr)
        return [(x["shape"], x["sessions"], x["count"]) for x in rows]

    def env_commands(self):
        """The 4 env-load chains over 6 sessions (chains 1 and 2 twice each), as --commands input."""
        return self.write_commands([{"ts": "2026-04-02T12:00:00Z", "session": f"s{i}", "cmd": ENV_CHAINS[i % 4]}
                                    for i in range(6)])

    def test_env_load_chains_collapse_to_one_step_row(self):
        """Chains that load `.env` among shell basics give one `. .env` row with its true session count, and
        no row for cat, export, set, mkdir, cp, git add or git commit.

        Whole-command shapes spread one chore over a shape per chain, so `. .env` in 6 sessions showed as
        rows of 1 or 2 sessions; a naive step split would instead put the shell basics on top. Neither
        tells anyone which chore to wrap.
        """
        # GIVEN 4 different env-load chains typed in 6 sessions
        cmds = self.env_commands()
        # WHEN the report ranks steps (the default unit)
        # THEN `. .env` is the only row, seen in all 6 sessions
        self.assertEqual(self.rows("--commands", str(cmds), "--covered", "none"), [(". .env", 6, 6)])

    def test_a_step_twice_in_one_run_counts_once(self):
        """A run that holds the same step twice adds 1 to that step's count, not 2.

        count is runs, the second ranking key after sessions; a chain that loads .env twice would
        otherwise double its chore's runs and outrank chores typed as often.
        """
        # GIVEN a chain loading .env twice, in 3 sessions
        cmds = self.write_commands([{"ts": "2026-04-02T12:00:00Z", "session": f"s{i}",
                                     "cmd": ". .env && x-tool run && . .env"} for i in range(3)])
        # WHEN the report runs
        # THEN `. .env` has 3 runs in 3 sessions
        self.assertIn((". .env", 3, 3), self.rows("--commands", str(cmds), "--covered", "none"))

    def test_unit_command_keeps_whole_shapes_and_basic_commands(self):
        """--unit command ranks whole command shapes as before steps existed: no step split, no basic drop.

        A caller that compares against older reports needs the old unit; a split or a dropped basic there
        would change numbers the caller thinks are like for like.
        """
        # GIVEN the env-load chains (no whole chain in 3 sessions), and `ls -la` in 3 sessions in the store
        cmds = self.env_commands()
        self.rec("ls -la", 3)
        # WHEN the report runs with --unit command over each, and over the store with the default unit
        # THEN no chain repeats as a whole, `ls -la` is listed whole, and the step unit drops it as a read
        self.assertEqual(self.rows("--commands", str(cmds), "--covered", "none", "--unit", "command"), [])
        self.assertEqual(self.rows("--since", "3650d", "--unit", "command"), [("ls -la", 3, 3)])
        self.assertEqual(self.rows("--since", "3650d"), [])

    def test_old_rows_without_steps_fall_back_to_steps_of_shape(self):
        """A store row written before rows carried `steps` is split from its shape, and its steps count
        together with the same steps of new rows.

        The store keeps 45 days of rows from the old recorder; skipping them would empty the report for
        two weeks, and keying them apart would split one chore into two rows.
        """
        # GIVEN two old rows (shape only, a path in it) and one new row of the same chore, in 3 sessions
        self.rec("set -a && deploy-tool ./dist", ["s2"])
        for s in ("s0", "s1"):
            record.append_line(self.store / "2026-04-02.jsonl", json.dumps(
                {"ts": "2026-04-02T12:00:00Z", "session": s, "shape": "set -a; deploy-tool <path> 2>&1",
                 "rules": [], "bypassed": []}))
        # WHEN the report runs
        # THEN one row holds all 3 sessions, the path folded and the redirect dropped as in steps()
        self.assertEqual(self.rows("--since", "3650d"), [("deploy-tool <arg>", 3, 3)])

    def test_a_step_is_covered_when_half_its_runs_matched_a_rule(self):
        """Under --covered live a step is dropped when a rule matched at least half of the runs holding it;
        one in four leaves it listed.

        A rule matches the whole command, so every step of a ruled run counts as ruled; with the half bar a
        chore that a rule caught once inside some other chain still shows.
        """
        # GIVEN a raw-text rule, one chain ruled in 2 of 4 sessions and another ruled in 1 of 4
        rules = json.dumps([{"id": "demo-rule", "mode": "warn", "on": "raw", "match": "DEMO_TOKEN",
                             "message": "use the demo command"}])
        (self.p / ".claude/agent-cli-hooks/pretooluse-rules.json").write_text(rules)
        rows = []
        for i in range(4):
            rows += [{"ts": "2026-04-02T12:00:00Z", "session": f"s{i}",
                      "cmd": "set -a && deploy-tool push" + ("  # DEMO_TOKEN" if i < 2 else "")},
                     {"ts": "2026-04-02T12:00:00Z", "session": f"s{i}",
                      "cmd": "set -a; ship-tool go" + ("  # DEMO_TOKEN" if i < 1 else "")}]
        cmds = self.write_commands(rows)
        # WHEN the report runs live and with none
        # THEN live keeps only the 1-in-4 step; none keeps both
        self.assertEqual(self.shapes("--commands", str(cmds)), ["ship-tool <arg>"])
        self.assertEqual(self.shapes("--commands", str(cmds), "--covered", "none"),
                         ["deploy-tool <arg>", "ship-tool <arg>"])

    def test_a_step_whose_command_word_is_covered_is_dropped(self):
        """Under --covered live a step whose command is on the `commands:` line is dropped, even inside a
        chain whose first command is something else.

        Before steps the command word was the chain's first word (`set`), so a project CLI typed after
        `set -a &&` was never seen as covered.
        """
        # GIVEN a project CLI typed after a basic in 3 sessions, and project-rules.md naming it
        self.rec("set -a && tasker-tool sync", 3)
        (self.p / ".claude/project-rules.md").write_text("## repeats\ncommands: tasker-tool\n")
        # WHEN the report runs live and with none
        # THEN dropped live, listed with none
        self.assertEqual(self.shapes("--since", "3650d"), [])
        self.assertEqual(self.shapes("--since", "3650d", "--covered", "none"), ["tasker-tool <arg>"])

    def test_report_lists_sleep_and_on_main_checks_but_no_basic_steps(self):
        """The report lists `sleep <n>` and `git log` steps that name origin/<arg> next to real chores, and
        drops the other basics (`set -a`, `cat <arg>`).

        Waiting and checking what is on main are chores an agent repeats by hand and a command can do;
        they are basic for the warning, but the report must still be able to rank them.
        """
        # GIVEN a chain of chores and basics in 3 sessions
        self.rec(CHORES, 3)
        # WHEN the report runs
        # THEN the env load, the tool, both on-main checks and the sleep are listed, and no other basic
        self.assertEqual(self.shapes("--since", "3650d"),
                         [". .env", "deploy-tool <arg>", "git log <arg>..origin/<arg>",
                          "git log origin/<arg> --oneline", "sleep <n>"])

    def test_cache_leaves_out_basic_and_report_only_steps(self):
        """--write-cache keys the cache by step and leaves out every basic step, sleep and on-main checks
        included.

        The warning reads this cache; a `sleep 30` or `git log origin/main..` on it would tell agents to
        wrap a wait or a read in a new command on almost every session.
        """
        # GIVEN the same chain of chores and basics in 3 sessions
        self.rec(CHORES, 3)
        # WHEN the cache is written
        r, _ = self.repeats("--since", "3650d", "--write-cache")
        # THEN it holds only the env load and the tool, each with its counts
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads((self.store / "repeats-cache.json").read_text()),
                         {". .env": {"sessions": 3, "count": 3}, "deploy-tool <arg>": {"sessions": 3, "count": 3}})


PY_DB = "python3 - <<'EOF'\nimport os; os.environ['DATABASE_URL']\nEOF"  # an inline query: a basic step
DEMO_RULE = json.dumps([{"id": "demo-rule", "mode": "warn", "on": "raw", "match": "DEMO_TOKEN",
                         "message": "use the demo command"}])


class Effects(StoreCase):
    def effects(self, *args):
        r, rows = self.repeats("--by", "effect", *args)
        self.assertEqual(r.returncode, 0, r.stderr)
        return rows

    def commands(self, pairs):
        """--commands input from (cmd, sessions) pairs, each cmd once in sessions s0..s<n-1>."""
        return self.write_commands([{"ts": "2026-04-02T12:00:00Z", "session": f"s{i}", "cmd": c}
                                    for c, n in pairs for i in range(n)])

    def test_effects_rank_by_sessions_then_runs_and_top_cuts(self):
        """--by effect lists each effect seen in 3+ sessions with sessions, count, first and last, ranked by
        sessions then runs; --top keeps the first N.

        One chore has many spellings but one host or variable; the effect row is what joins them, so its
        counts must be exact and its rank stable.
        """
        # GIVEN a host in 4 sessions, a name in 3 sessions twice each, an env file in 3, a host in 2
        self.rec("curl -s https://api.example.test/v1", 4)
        for _ in range(2):
            self.rec("ship-tool go $SHIP_KEY", 3)
        self.rec(". ./prod.env", 3)
        self.rec("curl https://rare.example.test", 2)
        # WHEN the report runs by effect, then with --top 2
        rows = self.effects("--since", "3650d", "--covered", "none")
        top = self.effects("--since", "3650d", "--covered", "none", "--top", "2")
        # THEN the order is by sessions then runs; the 2-session host is not listed
        self.assertEqual([(x["effect"], x["sessions"], x["count"]) for x in rows],
                         [("host:api.example.test", 4, 4), ("env:SHIP_KEY", 3, 6), ("envfile:prod.env", 3, 3)])
        self.assertEqual(set(rows[0]), {"effect", "sessions", "count", "first", "last", "steps"})
        self.assertEqual((rows[0]["first"], rows[0]["last"]), ("2026-04-02T12:00:00Z", "2026-04-02T12:00:00Z"))
        self.assertEqual([x["effect"] for x in top], ["host:api.example.test", "env:SHIP_KEY"])

    def test_steps_column_holds_the_3_most_common_carrier_steps(self):
        """steps holds the 3 most common listed steps among the runs carrying the effect; basic steps of a
        run (set -a) are left out.

        The steps say which chore the effect belongs to, so the reader knows what command to write.
        """
        # GIVEN DATABASE_URL read by psql in 4 sessions, pg_dump in 3, a tool after `set -a` in 2, python in 1
        cmds = self.commands([('psql "$DATABASE_URL" -c \'select 1\'', 4), ('pg_dump "$DATABASE_URL" > out.sql', 3),
                              ("set -a && dbtool sync $DATABASE_URL", 2), (PY_DB, 1)])
        # WHEN the report runs by effect over the commands
        rows = self.effects("--commands", str(cmds), "--covered", "none")
        # THEN the variable's row, its steps the three most common; and db:postgres from the Postgres CLI runs
        self.assertEqual([(x["effect"], x["sessions"], x["count"], x["steps"]) for x in rows],
                         [("env:DATABASE_URL", 4, 10, ["psql <str> -c <str>", "pg_dump <str>", "dbtool <arg>"]),
                          ("db:postgres", 4, 7, ["psql <str> -c <str>", "pg_dump <str>"])])

    def test_an_inline_code_carrier_shows_its_own_step(self):
        """An effect carried only by inline interpreter code lists that code's own step (`python3 - <heredoc>`).

        Inline code is basic for the step report, but it is how dev-DB queries run; an empty steps column
        would hide which kind of run reads the variable.
        """
        # GIVEN an inline python query in 3 sessions
        cmds = self.commands([(PY_DB, 3)])
        # WHEN the report runs by effect
        rows = self.effects("--commands", str(cmds), "--covered", "none")
        # THEN its own step is the carrier
        self.assertEqual([(x["effect"], x["steps"]) for x in rows], [("env:DATABASE_URL", ["python3 - <heredoc>"])])

    def test_effect_covered_live_when_half_its_runs_have_every_listed_step_covered(self):
        """Under --covered live an effect is dropped when at least half its runs have every listed step covered
        by the step test (a rule matched half the step's runs); others stay.

        The step test is what the step report uses; using it here keeps the two reports agreeing on what a
        command already covers.
        """
        # GIVEN a step ruled in 3 of its 6 runs, where only the 3 unruled runs read DEPLOY_KEY, and an
        # unruled name in 3 sessions
        (self.p / ".claude/agent-cli-hooks/pretooluse-rules.json").write_text(DEMO_RULE)
        cmds = self.write_commands(
            [{"ts": "2026-04-02T12:00:00Z", "session": f"s{i}",
              "cmd": "deploy-tool push  # DEMO_TOKEN" if i < 3 else "deploy-tool push $DEPLOY_KEY"} for i in range(6)]
            + [{"ts": "2026-04-02T12:00:00Z", "session": f"s{i}", "cmd": "ship-tool go $SHIP_KEY"} for i in range(3)])
        # WHEN the report runs by effect, live and with none
        live = self.effects("--commands", str(cmds))
        none = self.effects("--commands", str(cmds), "--covered", "none")
        # THEN live drops DEPLOY_KEY (its step is covered though none of its own runs was ruled); none keeps both
        self.assertEqual([x["effect"] for x in live], ["env:SHIP_KEY"])
        self.assertEqual([x["effect"] for x in none], ["env:DEPLOY_KEY", "env:SHIP_KEY"])

    def test_a_run_with_only_basic_steps_falls_back_to_its_own_rule_hits(self):
        """A run with no listed step (inline code only) counts as covered when a rule matched that run itself.

        Inline code has no listed step to test; without the fallback such effects could never be covered.
        """
        # GIVEN two inline-code effects in 3 sessions: one ruled in 2 of its runs, one in 1
        (self.p / ".claude/agent-cli-hooks/pretooluse-rules.json").write_text(DEMO_RULE)
        rows = []
        for i in range(3):
            rows += [{"ts": "2026-04-02T12:00:00Z", "session": f"s{i}",
                      "cmd": "python3 - <<'EOF'" + ("  # DEMO_TOKEN" if i < 2 else "")
                      + "\nimport os; os.environ['DB_URL']\nEOF"},
                     {"ts": "2026-04-02T12:00:00Z", "session": f"s{i}",
                      "cmd": "node -e 'process.env.API_URL'" + ("  # DEMO_TOKEN" if i < 1 else "")}]
        cmds = self.write_commands(rows)
        # WHEN the report runs by effect, live
        # THEN only the 1-in-3 effect stays
        self.assertEqual([x["effect"] for x in self.effects("--commands", str(cmds))], ["env:API_URL"])

    def test_an_effect_exactly_half_covered_is_covered(self):
        """An effect whose runs are exactly half covered (2 of 4) is dropped under --covered live; 1 of 4 stays.

        The step report drops a step at half its runs ruled; the effect report must use the same boundary or
        the two reports disagree on a tie.
        """
        # GIVEN HALF_KEY read by a covered step in 2 of its 4 runs, LOW_KEY in 1 of its 4
        (self.p / ".claude/agent-cli-hooks/pretooluse-rules.json").write_text(DEMO_RULE)
        cmds = self.write_commands(
            [{"ts": "2026-04-02T12:00:00Z", "session": f"s{i}", "cmd": c}
             for i, c in enumerate(["deploy-tool push $HALF_KEY  # DEMO_TOKEN"] * 2 + ["other-tool $HALF_KEY"] * 2
                                   + ["deploy-tool push $LOW_KEY  # DEMO_TOKEN"] + ["other-tool $LOW_KEY"] * 3)])
        # WHEN the report runs by effect, live, from 1 session
        rows = self.effects("--commands", str(cmds), "--min-sessions", "1")
        # THEN only the 1-in-4 effect stays
        self.assertEqual([x["effect"] for x in rows], ["env:LOW_KEY"])

    def test_first_and_last_span_the_earliest_and_latest_run(self):
        """first and last are the earliest and latest run of the effect, whatever order the rows come in.

        A reader uses them to tell a chore that is still going from one that stopped.
        """
        # GIVEN the later run first
        rows = [{"ts": "2026-04-03T00:00:00Z", "session": "a", "steps": ["tool <arg>"], "effects": ["env:K_A"]},
                {"ts": "2026-04-01T00:00:00Z", "session": "b", "steps": ["tool <arg>"], "effects": ["env:K_A"]}]
        # WHEN grouped
        g = repeats.group_effects(rows)["env:K_A"]
        # THEN first is the earlier, last the later
        self.assertEqual((g["first"], g["last"]), ("2026-04-01T00:00:00Z", "2026-04-03T00:00:00Z"))

    def test_a_basic_only_run_with_a_bypassed_rule_counts_as_covered(self):
        """A run with only basic steps whose rule was skipped by an allow comment (bypassed) counts as covered.

        A bypassed rule still matched the run: the chore has a rule, so it must not show as uncovered.
        """
        # GIVEN an inline-code run whose only rule hit was bypassed, and one with no rule hit
        rows = [{"ts": "2026-04-01T00:00:00Z", "session": "a", "steps": ["python3 - <heredoc>"],
                 "effects": ["env:K_A"], "rules": [], "bypassed": ["demo-rule"]},
                {"ts": "2026-04-01T00:00:00Z", "session": "b", "steps": ["python3 - <heredoc>"],
                 "effects": ["env:K_B"], "rules": [], "bypassed": []}]
        # WHEN grouped with a step test
        g = repeats.group_effects(rows, lambda k: False)
        # THEN the bypassed run is covered and the other is not
        self.assertEqual((g["env:K_A"]["covered"], g["env:K_B"]["covered"]), (1, 0))

    def test_old_rows_without_effects_add_nothing_and_bad_effects_are_skipped(self):
        """A store row from before rows carried effects adds no effect; a row whose effects is not a list of
        strings is skipped and counted like a cut-short line.

        The store keeps 45 days of old rows; reading a missing field as an error would empty the report,
        and a hand-edited row must not crash it.
        """
        # GIVEN a recorded name in 3 sessions, an old row without effects and a row with a bad effects value
        self.rec("ship-tool go $SHIP_KEY", 3)
        for row in ({"ts": "2026-04-02T12:00:00Z", "session": "s9", "shape": "curl <url:old.example.test>",
                     "rules": [], "bypassed": []},
                    {"ts": "2026-04-02T12:00:00Z", "session": "s8", "shape": "x", "effects": "env:SHIP_KEY"}):
            record.append_line(self.store / "2026-04-02.jsonl", json.dumps(row))
        # WHEN the report runs by effect
        r, rows = self.repeats("--by", "effect", "--since", "3650d", "--covered", "none", "--min-sessions", "1")
        # THEN only the recorded name, and one skipped line
        self.assertEqual([(x["effect"], x["sessions"]) for x in rows], [("env:SHIP_KEY", 3)])
        self.assertIn("1 unreadable store lines skipped", r.stderr)

    def test_by_effect_refuses_write_cache_and_unit_command(self):
        """--by effect with --write-cache or --unit command exits 2 with an example; an unknown --by too.

        Effects never feed the live warning (a third of raw hits are text, not calls), and they have no
        whole-command unit; silently ignoring either flag would mislead the caller.
        """
        self.store.mkdir()
        for args in (("--by", "effect", "--write-cache"), ("--by", "effect", "--unit", "command"), ("--by", "step")):
            with self.subTest(args=args):
                r, _ = self.repeats(*args)
                self.assertEqual(r.returncode, 2, r.stderr)
                self.assertIn("example:", r.stderr)

    def test_text_mode_prints_effect_table_with_steps_joined(self):
        """Without --json the effect report is a header then one line per effect, steps joined by `  //  `
        (not shell syntax, so a reader cannot take two steps for one pipeline).

        house-rules: text output carries the same fields as --json.
        """
        # GIVEN a name read by two tools in 3 sessions
        self.rec("ship-tool go $SHIP_KEY && ship-check $SHIP_KEY", 3)
        # WHEN the report runs by effect in text mode
        r = agent_cli(self.p, "repeats", "--by", "effect", "--since", "3650d", "--covered", "none",
                      env={"AGENT_CLI_SHAPES_DIR": str(self.store)})
        # THEN the header names every field, and the row ends with the joined steps
        head, row = r.stdout.splitlines()
        self.assertEqual(head.split(), ["sessions", "count", "first", "last", "effect", "steps"])
        self.assertEqual(row, "3  3  2026-04-02T12:00:00Z  2026-04-02T12:00:00Z  env:SHIP_KEY  "
                              "ship-check <arg>  //  ship-tool <arg>")


class Speed(StoreCase):
    def test_20000_commands_replay_in_under_10s(self):
        """--commands over 20,000 raw commands finishes in under 10 s (the replay cap per window).

        Test A replays two windows of up to 20,000 commands each on the laptop.
        """
        # GIVEN 20,000 commands of 50 kinds across 400 sessions
        kinds = ["git log --oneline -n {i}", "curl -s https://h{i}.example.com/x", "psql -c 'select {i}'",
                 "gh pr view {i} --json state", POLL]
        rows = [{"ts": "2026-04-02T12:00:00Z", "session": f"s{i % 400}",
                 "cmd": kinds[i % 5].format(i=i) + f" --k{i % 10}"} for i in range(20000)]
        cmds = self.write_commands(rows)
        # WHEN the report runs over them
        t = time.monotonic()
        got = self.shapes("--commands", str(cmds))
        took = time.monotonic() - t
        # THEN it listed shapes and took under 10 s
        self.assertTrue(got)
        self.assertLess(took, 10.0)

    def test_write_cache_over_110000_rows_in_under_4s(self):
        """--write-cache --since 14d over a 110,000-row store finishes in under 4 s.

        The warning hook writes this cache at session start inside a 5 s timeout; about 14 days of a
        busy repo is 110,000 rows.
        """
        # GIVEN 400 rows made by record.record, copied into 110,000 rows over the last 14 days
        seed = self.tmp / "seed"
        set_env(self, AGENT_CLI_SHAPES_DIR=str(seed))
        now = dt.datetime.now(UTC)
        for i in range(400):
            record.record(post_event(f"tool{i % 80} run --flag {i}", session=f"s{i}", cwd=None), now=now)
        template = store_rows(seed)
        self.store.mkdir()
        files = {}
        for i in range(110000):
            at = now - dt.timedelta(seconds=i * 13 * 86400 // 110000)
            row = dict(template[i % len(template)], ts=at.strftime("%Y-%m-%dT%H:%M:%SZ"), session=f"s{i % 900}")
            files.setdefault(at.strftime("%Y-%m-%d"), []).append(json.dumps(row) + "\n")
        for day, lines in files.items():
            (self.store / f"{day}.jsonl").write_text("".join(lines))
        # WHEN the report writes the cache
        t = time.monotonic()
        r, rows = self.repeats("--since", "14d", "--write-cache")
        took = time.monotonic() - t
        # THEN the cache is written and it took under 4 s
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(json.loads((self.store / "repeats-cache.json").read_text())), 80)
        self.assertLess(took, 4.0)


if __name__ == "__main__":
    unittest.main()
