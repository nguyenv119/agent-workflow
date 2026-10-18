"""Tests for agent_cli.record and the `hook posttooluse|posttoolusefailure` verbs (the shape recorder)."""
import datetime as dt
import json
import os
import sys
import unittest

from helpers import CLAUDE, GIT, TmpCase, agent_cli, make_project, run, set_env, store_rows
from helpers import post_event as event

sys.path.insert(0, str(CLAUDE / "cli"))
from agent_cli import record  # noqa: E402

SECRET = "sk-live-PLANTED0123456789"
WORD = "correcthorsebatterystaple"  # a secret with no digits and no capitals
NOW = dt.datetime(2026, 10, 14, 5, 6, 7, tzinfo=dt.timezone.utc)
SEAMS = {"command-prefixes": "# wrappers\nwrapx\nwrapx raw\n", "subcommand-tools": "tasker\n",
         "pretooluse-rules.json": json.dumps([{"id": "raw-sql-demo", "mode": "warn", "match": "{CMD}psql\\b",
                                               "message": "use the sql command"}])}


class Recorder(TmpCase):
    """record() in process, with the store pointed at a temp dir."""

    def setUp(self):
        super().setUp()
        self.store = self.tmp / "shapes"
        set_env(self, AGENT_CLI_SHAPES_DIR=str(self.store), AGENT_CLI_SHAPES_KEEP_DAYS=None)

    def rows(self, day="2026-10-14"):
        f = self.store / f"{day}.jsonl"
        return [json.loads(ln) for ln in f.read_text().splitlines()] if f.exists() else []

    def test_row_has_the_fields_and_lands_in_the_day_file(self):
        """One event appends one JSON line with ts, session, agent, repo, shape, exit and rules.

        The report (agent-cli repeats) counts sessions per shape from these rows; a missing field means
        it cannot tell sessions or repos apart.
        """
        # GIVEN a successful Bash event from a subagent, in a git repo's subdir
        repo = make_project(self.tmp / "myrepo", install=False)
        (repo / "sub").mkdir()
        # WHEN recorded
        row = record.record(json.dumps(event("git status", cwd=repo / "sub", agent_id="ag-1")), now=NOW)
        # THEN the row is returned and is the only line in the day file
        want = {"ts": "2026-10-14T05:06:07Z", "session": "sess-a", "agent": "ag-1", "repo": "myrepo",
                "shape": "git status", "steps": ["git status"], "exit": 0, "rules": [], "bypassed": []}
        self.assertEqual(row, want)
        self.assertEqual(self.rows(), [want])

    def test_repo_falls_back_to_the_cwd_basename_outside_git(self):
        """Outside a git repo, repo is the cwd's own name, so rows still say where they ran.

        Without it, commands run outside any repo would carry repo null and a per-repo report would drop them.
        """
        # GIVEN a plain dir
        d = self.tmp / "plain-dir"
        d.mkdir()
        # WHEN recorded
        row = record.record(event("ls", cwd=d), now=NOW)
        # THEN repo is the dir's name
        self.assertEqual(row["repo"], "plain-dir")

    def test_repo_in_a_linked_worktree_is_the_main_repo(self):
        """A linked worktree records the main repo's name, so one repo's sessions are counted together.

        Every bead runs in its own worktree dir; keying on the worktree dir would split one repo's chores
        across a name per bead.
        """
        # GIVEN a repo with one commit and a linked worktree beside it
        repo = make_project(self.tmp / "mainrepo", install=False)
        run(GIT + ["commit", "-q", "--allow-empty", "-m", "init"], repo)
        run(GIT + ["worktree", "add", "-q", str(self.tmp / "mainrepo-bead-1"), "-b", "b1"], repo)
        # WHEN an event from the worktree is recorded
        row = record.record(event("ls", cwd=self.tmp / "mainrepo-bead-1"), now=NOW)
        # THEN repo is the main repo's name
        self.assertEqual(row["repo"], "mainrepo")

    def test_failure_exit_code_is_parsed(self):
        """PostToolUseFailure's "Exit code N" becomes exit N; no code (an interrupt) becomes null.

        A report that tells failing chores from working ones needs the real code; guessing 1 for an
        interrupt would count a user's Ctrl-C as a failed command.
        """
        # GIVEN a failure with a code and one without
        failed = event("ls /nope", name="PostToolUseFailure", error="Exit code 2\nls: /nope: No such file")
        cut = event("sleep 9", name="PostToolUseFailure", error="interrupted", is_interrupt=True)
        # WHEN both are recorded
        # THEN the code is 2, then null
        self.assertEqual(record.record(failed, now=NOW)["exit"], 2)
        self.assertIsNone(record.record(cut, now=NOW)["exit"])

    def test_unparseable_command_is_recorded_with_a_null_shape(self):
        """A command shape() cannot parse still gets a row, with shape null and nothing of the command.

        Dropping the row would hide how often parsing fails; storing any text would break the privacy rule.
        """
        # GIVEN an unbalanced quote carrying a value
        # WHEN recorded
        row = record.record(event('echo "unclosed value'), now=NOW)
        # THEN shape is null and the file never holds the text
        self.assertIsNone(row["shape"])
        self.assertNotIn("unclosed", (self.store / "2026-10-14.jsonl").read_text())

    def test_non_bash_events_are_ignored(self):
        """Only Bash is recorded; any other tool writes nothing."""
        # GIVEN a Read event
        e = event("x")
        e["tool_name"] = "Read"
        # WHEN recorded
        # THEN no row and no store
        self.assertIsNone(record.record(e, now=NOW))
        self.assertFalse(self.store.exists())

    def test_long_shape_is_truncated_to_1kb(self):
        """A shape over 1 KB is cut at 1 KB, so each line stays one atomic append under 4 KB.

        Appends over the pipe-buffer size can interleave between concurrent sessions and corrupt rows.
        """
        # GIVEN a command with 600 distinct segments
        cmd = " && ".join(f"t{i} -x" for i in range(600))
        # WHEN recorded
        row = record.record(event(cmd), now=NOW)
        # THEN the shape is 1024 chars and the line well under 4 KB
        self.assertEqual(len(row["shape"]), 1024)
        self.assertLess(len((self.store / "2026-10-14.jsonl").read_bytes()), 4096)

    def test_planted_secret_is_stored_nowhere(self):
        """No part of a secret typed anywhere in a command, or printed by it, reaches the store.

        The recorder runs on every Bash call an agent makes, including ones that carry tokens; the store
        is a plain file under the home dir. If any 6+ char piece of the secret survives, the store is a
        credential leak.
        """
        # GIVEN 8 commands with the secret in a different spot each, 5 more with a digit-free secret in the
        # slots that keep words (first argument, glued short flag, after a stripped prefix, base64 padding,
        # an unterminated-looking heredoc), plus output that carries both
        commands = [f"SECRET={SECRET} make deploy",
                    f'curl -H "Authorization: Bearer {SECRET}" https://api.example.com',
                    f"tool {SECRET}",
                    f"curl https://api.example.com/x?key={SECRET}",
                    f"curl https://api.example.com/{SECRET}/x",
                    f"deploy --token {SECRET}",
                    f"cat <<EOF\n{SECRET}\nEOF",
                    f"export API_KEY={SECRET}"]
        commands += [f"echo {WORD}", f"tool -p{WORD}", f"command -v {WORD}", f"echo {WORD}== | base64 -d",
                     f"cat <<$D\n{WORD}\n$D"]
        out = {"stdout": f"token={SECRET} {WORD}", "stderr": SECRET, "interrupted": False}
        # WHEN each is recorded as a success, and once as a failure whose error carries it
        for c in commands:
            record.record(event(c, tool_response=out), now=NOW)
        record.record(event(commands[0], name="PostToolUseFailure", error=f"Exit code 1\n{SECRET}"), now=NOW)
        # THEN 9 rows exist and no 6+ char substring of the secret is in any byte under the store
        self.assertEqual(len(self.rows()), 14)
        blob = b"".join(f.read_bytes() for f in self.store.rglob("*") if f.is_file()).lower()
        pieces = {x[i:j] for x in (SECRET.lower(), WORD) for i in range(len(x)) for j in range(i + 6, len(x) + 1)}
        self.assertEqual([p for p in sorted(pieces) if p.encode() in blob], [])

    def test_old_day_files_are_deleted_once_a_day(self):
        """Day files older than the keep window go; the sweep runs at most once a day (marker file).

        Without retention the store grows by thousands of rows a day forever; without the marker every
        Bash call would list the dir.
        """
        # GIVEN files 50, 46, 45 and 10 days old, and a non-day file
        self.store.mkdir()
        old, recent, other = (self.store / "2026-08-25.jsonl", self.store / "2026-10-04.jsonl",
                              self.store / "notes.txt")
        day46, day45 = self.store / "2026-08-29.jsonl", self.store / "2026-08-30.jsonl"
        for f in (old, recent, other, day46, day45):
            f.write_text("{}\n")
        # WHEN one event is recorded, then a new old file appears and a second event is recorded
        record.record(event("ls"), now=NOW)
        late = self.store / "2026-08-01.jsonl"
        late.write_text("{}\n")
        record.record(event("ls"), now=NOW)
        # THEN the first old file is gone, the recent and other files stay, and the late one survives today
        self.assertEqual((old.exists(), recent.exists(), other.exists(), late.exists()), (False, True, True, True))
        # AND at the boundary the 46-day-old file went and the 45-day-old one stayed
        self.assertEqual((day46.exists(), day45.exists()), (False, True))
        # AND the next day's first event sweeps the late one
        record.record(event("ls"), now=NOW + dt.timedelta(days=1))
        self.assertFalse(late.exists())

    def test_sweep_deletes_old_warned_once_marks(self):
        """The daily sweep also deletes <store>/warned/ files (the repeat warning's once-per-session marks)
        last written before the keep window; newer ones stay.

        One file per session would otherwise pile up forever.
        """
        # GIVEN a mark last written 50 days ago and one written yesterday
        warned = self.store / "warned"
        warned.mkdir(parents=True)
        old, recent = warned / "old-session.txt", warned / "new-session.txt"
        for f, days in ((old, 50), (recent, 1)):
            f.write_text("[]\n")
            t = (NOW - dt.timedelta(days=days)).timestamp()
            os.utime(f, (t, t))
        # WHEN an event is recorded
        record.record(event("ls"), now=NOW)
        # THEN the old mark is gone and the recent one stays
        self.assertEqual((old.exists(), recent.exists()), (False, True))

    def test_keep_days_comes_from_the_env(self):
        """AGENT_CLI_SHAPES_KEEP_DAYS sets the window (5 here deletes a 10-day-old file).

        If the env var were ignored, a user who must keep less history could not shrink the store.
        """
        # GIVEN a 10-day-old file and a 5-day window
        self.store.mkdir()
        f = self.store / "2026-10-04.jsonl"
        f.write_text("{}\n")
        os.environ["AGENT_CLI_SHAPES_KEEP_DAYS"] = "5"
        # WHEN an event is recorded
        record.record(event("ls"), now=NOW)
        # THEN the file is gone
        self.assertFalse(f.exists())

    def test_steps_hold_each_step_and_each_substitution_body(self):
        """steps lists the command's step shapes in order, then the steps of each $( ) body, beside the
        unchanged whole shape.

        The report counts a chore per step; without steps in the row it would re-split every stored shape
        and could never see inside a $( ) body, whose text is gone once the shape is stored.
        """
        # GIVEN a chain that reads a key in a substitution, loads .env and runs a tool on a path
        cmd = "TOKEN=$(grep KEY .env | cut -d= -f2) && . .env && deploy-tool push ./dist"
        # WHEN recorded
        row = record.record(event(cmd, cwd=None), now=NOW)
        # THEN the assignment-only step is dropped, the path folded, and the body's pipeline added last
        self.assertEqual(row["steps"], [". .env", "deploy-tool <arg>", "grep <arg> .env | cut -d=<v> -f<v>"])
        self.assertEqual(row["shape"], "TOKEN=<v> && . .env && deploy-tool <arg> <path>")

    def test_steps_stop_at_32(self):
        """A command with hundreds of distinct steps stores the first 32, so a row stays a short line.

        Rows are appended with one write by many sessions at once; an unbounded steps list from a long
        generated script would make lines that can interleave and bloat every report read.
        """
        # GIVEN a command with 600 distinct steps
        cmd = " && ".join(f"t{i} -x" for i in range(600))
        # WHEN recorded
        row = record.record(event(cmd, cwd=None), now=NOW)
        # THEN 32 steps, the first 32 in order
        self.assertEqual(row["steps"], [f"t{i} -x" for i in range(32)])

    def test_planted_db_password_in_steps_reaches_no_store_byte(self):
        """A database password typed as PGPASSWORD=..., inside a $( ) body or in a connection URL, never
        reaches the store through the hook verb, steps included.

        steps shape $( ) bodies on their own, a new path for raw text to reach the store; a password that
        survived there would sit in a plain file under the home dir for 45 days.
        """
        # GIVEN a project, and 4 commands carrying the same planted password
        p = make_project(self.tmp / "proj", seams=SEAMS)
        plant = "s3cr3t-plant-9"
        commands = [f"PGPASSWORD={plant} psql -h db.example.test -c 'select 1'",
                    f"export PGPASSWORD={plant} && . .env && psql -c 'select 1'",
                    f"X=$(echo PGPASSWORD={plant}) && deploy-tool push",
                    f'psql "postgresql://u:{plant}@db.example.test/x" && deploy-tool push']
        # WHEN each is recorded through the hook verb
        for c in commands:
            r = agent_cli(p, "hook", "posttooluse", stdin=json.dumps(event(c, cwd=None)),
                          env={"AGENT_CLI_SHAPES_DIR": str(self.store)})
            self.assertEqual(r.returncode, 0, r.stderr)
        # THEN 4 rows with steps exist, and no 6+ char piece of the password is in any byte under the store
        rows = store_rows(self.store)
        self.assertEqual(len(rows), 4)
        self.assertIn("echo PGPASSWORD=<v>", rows[2]["steps"])
        blob = b"".join(f.read_bytes() for f in self.store.rglob("*") if f.is_file())
        pieces = {plant[i:j] for i in range(len(plant)) for j in range(i + 6, len(plant) + 1)}
        self.assertEqual([x for x in sorted(pieces) if x.encode() in blob], [])

    def test_matching_hook_rules_are_listed_by_id(self):
        """rules names the active hook rules that match the raw command, computed before it is discarded.

        The report uses this to tell a shape a rule already covers from an uncovered one without ever
        storing the raw command.
        """
        # GIVEN a project with a warn rule on psql, and the CLI's hook verb
        p = make_project(self.tmp / "proj", seams=SEAMS)
        # WHEN a wrapped psql call is recorded through the hook verb
        r = agent_cli(p, "hook", "posttooluse", stdin=json.dumps(event("wrapx raw psql -c 'select 1'", cwd=p)),
                      env={"AGENT_CLI_SHAPES_DIR": str(self.store)})
        # THEN nothing printed, exit 0, and the row has the wrapper stripped and the rule id
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))
        (row,) = store_rows(self.store)
        self.assertEqual((row["shape"], row["rules"], row["repo"]), ("psql -c <str>", ["raw-sql-demo"], "proj"))

    def test_a_bypassed_rule_is_listed_under_bypassed_not_rules(self):
        """A rule skipped by `# agent-cli: allow <id>` goes in bypassed; rules keeps only the rules that fired.

        A deny rule blocks its command, so the recorder only sees a denied chore when an agent bypassed it.
        The report counts such a shape as covered (a command for it exists), which it can only do if the
        row says which rule was bypassed; the raw comment is never stored.
        """
        # GIVEN a project with a warn rule on psql, and a psql call that bypasses it
        p = make_project(self.tmp / "proj", seams=SEAMS)
        # WHEN recorded through the hook verb, with and without the bypass comment
        for cmd in ("psql -c 'select 1'  # agent-cli: allow raw-sql-demo", "psql -c 'select 1'"):
            agent_cli(p, "hook", "posttooluse", stdin=json.dumps(event(cmd, cwd=p)),
                      env={"AGENT_CLI_SHAPES_DIR": str(self.store)})
        # THEN the bypassed row names the rule under bypassed only; the other under rules only
        self.assertEqual([(r["shape"], r["rules"], r["bypassed"]) for r in store_rows(self.store)],
                         [("psql -c <str>", [], ["raw-sql-demo"]), ("psql -c <str>", ["raw-sql-demo"], [])])

    def test_seam_subcommand_tool_keeps_its_subcommand_on_disk(self):
        """A tool listed in the project's subcommand-tools seam keeps its second word in the stored row.

        The seam is read by the installed CLI from the project's .claude/; if the hook verb ignored it,
        a project could list its tools and still see them collapse in the store.
        """
        # GIVEN a project whose seam lists the made-up tool `tasker`
        p = make_project(self.tmp / "proj", seams=SEAMS)
        # WHEN a tasker call is recorded through the hook verb
        agent_cli(p, "hook", "posttooluse", stdin=json.dumps(event("tasker run build", cwd=p)),
                  env={"AGENT_CLI_SHAPES_DIR": str(self.store)})
        # THEN its subcommand is kept
        self.assertEqual([r["shape"] for r in store_rows(self.store)], ["tasker run <arg>"])


class HookVerbs(TmpCase):
    """The two hook verbs never fail a tool call and never print."""

    def setUp(self):
        super().setUp()
        self.p = make_project(self.tmp / "proj")

    def verb(self, name, stdin, store):
        return agent_cli(self.p, "hook", name, stdin=stdin, env={"AGENT_CLI_SHAPES_DIR": str(store)})

    def test_failure_verb_records_the_exit_code(self):
        """posttoolusefailure records the event with its exit code.

        This verb is what the PostToolUseFailure settings entry calls; if it did not record, no failing
        command would ever reach the store.
        """
        # GIVEN a failure event
        e = json.dumps(event("ls /nope", name="PostToolUseFailure", error="Exit code 1\nno such file"))
        # WHEN fed to the verb
        r = self.verb("posttoolusefailure", e, self.tmp / "s")
        # THEN one row with exit 1
        self.assertEqual((r.returncode, r.stdout), (0, ""))
        (row,) = store_rows(self.tmp / "s")
        self.assertEqual((row["shape"], row["exit"]), ("ls <path>", 1))

    def test_garbage_stdin_exits_0_silently(self):
        """Non-JSON or wrong-shaped stdin exits 0 with no output, for both verbs.

        A recorder that errors would show a hook failure on every tool call.
        """
        for verb in ("posttooluse", "posttoolusefailure"):
            for stdin in ("not json", "[]", '{"tool_name": "Bash"}', ""):
                with self.subTest(verb=verb, stdin=stdin):
                    # GIVEN garbage on stdin
                    # WHEN the verb runs
                    r = self.verb(verb, stdin, self.tmp / "s")
                    # THEN exit 0, nothing printed
                    self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))

    def test_unwritable_store_exits_0_silently(self):
        """A store that cannot be created or written exits 0 with no output.

        The store is under the home dir by default; a full disk or a read-only home must not break Bash.
        """
        # GIVEN a store path under a regular file, and a read-only store dir
        blocker = self.tmp / "file"
        blocker.write_text("x")
        ro = self.tmp / "ro"
        ro.mkdir()
        ro.chmod(0o500)
        self.addCleanup(ro.chmod, 0o700)
        for store in (blocker / "shapes", ro):
            with self.subTest(store=str(store)):
                # WHEN the verb runs
                r = self.verb("posttooluse", json.dumps(event("ls")), store)
                # THEN exit 0, nothing printed, nothing written
                self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))
        self.assertEqual(list(ro.iterdir()), [])


class Settings(unittest.TestCase):
    def test_settings_wire_both_events_async_and_guarded(self):
        """settings.json runs the recorder on PostToolUse and PostToolUseFailure for Bash, async, guarded.

        async keeps the recorder off the tool call's path; the guard and `exit 0` keep a project
        without agent-cli (or a broken one) from showing a hook error.
        """
        # GIVEN the harness settings
        hooks = json.loads((CLAUDE / "settings.json").read_text())["hooks"]
        for ev, verb in (("PostToolUse", "posttooluse"), ("PostToolUseFailure", "posttoolusefailure")):
            with self.subTest(event=ev):
                # WHEN the event's Bash entries are read
                entries = [h for g in hooks[ev] if g.get("matcher") == "Bash" for h in g["hooks"]
                           if f"hook {verb}" in h["command"]]
                # THEN exactly one, async, timeout 5, guarded and ending in exit 0
                self.assertEqual(len(entries), 1)
                (h,) = entries
                self.assertEqual((h.get("async"), h.get("timeout")), (True, 5))
                self.assertTrue(h["command"].startswith('[ -x "$CLAUDE_PROJECT_DIR/.claude/bin/agent-cli" ]'))
                self.assertTrue(h["command"].endswith("; exit 0"))


if __name__ == "__main__":
    unittest.main()
