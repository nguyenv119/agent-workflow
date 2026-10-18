"""Tests for agent_cli.record and the `hook posttooluse|posttoolusefailure` verbs (the shape recorder)."""
import datetime as dt
import json
import os
import signal
import sys
import unittest
from pathlib import Path

from helpers import CLAUDE, GIT, TmpCase, agent_cli, make_project, run, set_env, store_rows
from helpers import post_event as event

sys.path.insert(0, str(CLAUDE / "cli"))
from agent_cli import record  # noqa: E402
from agent_cli.effects import effects  # noqa: E402

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
                "shape": "git status", "steps": ["git status"], "effects": [], "exit": 0, "rules": [],
                "bypassed": []}
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
        self.assert_not_stored(plant)

    def assert_not_stored(self, plant):
        """No 6+ char piece of plant is in any byte under the store."""
        blob = b"".join(f.read_bytes() for f in self.store.rglob("*") if f.is_file())
        pieces = {plant[i:j] for i in range(len(plant)) for j in range(i + 6, len(plant) + 1)}
        self.assertEqual([x for x in sorted(pieces) if x.encode() in blob], [])

    def test_effects_failure_leaves_the_row_with_no_effects(self):
        """A command effects() cannot read (1000 nested `$(`) still gives its shape and steps, effects [].

        effects() is the newest, most intricate reader; a crash in it must not cost the row the report
        already counts.
        """
        # GIVEN a deeply nested command
        cmd = "$(" * 1000 + "x" + ")" * 1000
        # WHEN its fields are computed
        got = record.fields(cmd, [], (), ())
        # THEN the shape and steps are there and effects is empty
        self.assertEqual((got["shape"], got["steps"], got["effects"]), ("<str>", ["<str>"], []))

    def test_fields_carry_the_effects_of_the_raw_command(self):
        """fields() returns the command's effects (hosts, env var names, env file basenames) beside its shape.

        The weekly report ranks chores by what they touch; the raw text is gone once the row is written, so
        the effects must be read from it here, in memory, past the project's wrapper words.
        """
        # GIVEN a wrapped env load plus an API call
        cmd = 'wrapx . ../.env && curl -H "Authorization: Bearer $API_TOKEN" https://API.example.test:8443/v1'
        # WHEN its fields are computed
        got = record.fields(cmd, [], ("wrapx",), ())
        # THEN the effects are listed, sorted, with no value
        self.assertEqual(got["effects"], ["env:API_TOKEN", "envfile:.env", "host:api.example.test"])

    def test_planted_database_url_leaves_only_its_host_and_name(self):
        """A database URL with a planted password, in commands that only read DATABASE_URL, stores
        host:db.example.test and env:DATABASE_URL through the hook verb, and the password in no byte.

        Effects exist to name what a chore touches without its values; a password that slipped into an
        effect would sit in a plain file under the home dir for 45 days.
        """
        # GIVEN a project, and commands that read DATABASE_URL and carry the planted URL
        p = make_project(self.tmp / "proj", seams=SEAMS)
        plant = "s3cr3t-plant-7"
        url = f"postgres://u:{plant}@db.example.test/x"
        commands = [f"python3 - <<'EOF'\nimport os\nu = os.environ.get('DATABASE_URL', '{url}')\nEOF",
                    f"psql \"$DATABASE_URL\" -c 'select 1' && psql \"{url}\" -c 'select 2'",
                    f"wrapx node -e 'const u = process.env.DATABASE_URL || \"{url}\"'"]
        # WHEN each is recorded through the hook verb
        for c in commands:
            r = agent_cli(p, "hook", "posttooluse", stdin=json.dumps(event(c, cwd=None)),
                          env={"AGENT_CLI_SHAPES_DIR": str(self.store)})
            self.assertEqual(r.returncode, 0, r.stderr)
        # THEN every row holds the host and the name, and no 6+ char piece of the password is under the store
        rows = store_rows(self.store)
        self.assertEqual([r["effects"] for r in rows],
                         [["db:postgres", "env:DATABASE_URL", "host:db.example.test"]] * 3)
        self.assert_not_stored(plant)

    def test_planted_postgres_cli_password_leaves_only_the_db_signal_and_host(self):
        """A Postgres CLI call with PGPASSWORD assigned and -h/-U/-d flags stores db:postgres and the -h host
        through the hook verb: no env:PGPASSWORD (the command assigns it), no flag value, the password in no byte.

        The db signal is a constant so no flag value can ride along; a password that slipped through would sit
        in a plain file under the home dir for 45 days.
        """
        # GIVEN a project, and a CLI call carrying a planted password
        p = make_project(self.tmp / "proj", seams=SEAMS)
        plant = "s3cr3t-plant-8"
        cmd = f"PGPASSWORD={plant} psql -h db.example.test -U u -d x -c 'select 1'"
        # WHEN it is recorded through the hook verb
        r = agent_cli(p, "hook", "posttooluse", stdin=json.dumps(event(cmd, cwd=None)),
                      env={"AGENT_CLI_SHAPES_DIR": str(self.store)})
        # THEN exit 0, the row holds the signal and the host only, and no 6+ char piece of the password is stored
        self.assertEqual(r.returncode, 0, r.stderr)
        (row,) = store_rows(self.store)
        self.assertEqual(row["effects"], ["db:postgres", "host:db.example.test"])
        self.assert_not_stored(plant)

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


class Scripts(TmpCase):
    """fields()/record() reading the scripts a command runs: their steps and effects, never their text."""

    def setUp(self):
        super().setUp()
        self.store = self.tmp / "shapes"
        set_env(self, AGENT_CLI_SHAPES_DIR=str(self.store))
        self.cwd = self.tmp / "work"
        self.cwd.mkdir()

    def fields_of(self, command, **kw):
        return record.fields(command, [], (), (), **kw)

    def fx(self, command, **kw):
        return self.fields_of(command, **kw)["effects"]

    def test_bead_example_python_script_gives_its_env_read_and_env_file(self):
        """`python3 <dir>/q.py`, where q.py opens .env and reads DATABASE_URL, gives env:DATABASE_URL and
        envfile:.env beside the masked shape, and adds no steps.

        41% of hand-done chores sit in scripts the agent wrote then ran; the command line alone says only
        `python3 <path>/q.py`, so the repeat across sessions is invisible without the script's effects. A
        python body is not shell, so splitting it into steps would invent chores like `import <arg>`.
        """
        # GIVEN a written q.py
        q = self.cwd / "q.py"
        q.write_text("import os\nfrom dotenv import load_dotenv\nload_dotenv('.env')\n"
                     "u = os.environ['DATABASE_URL']\n")
        # WHEN the command's fields are computed with the event's cwd
        got = record.fields(f"python3 {q}", [], (), (), cwd=str(self.cwd))
        # THEN the script's effects join, and the steps are the command's own
        self.assertEqual((got["shape"], got["steps"], got["effects"]),
                         ("python3 <path>/q.py", ["python3 <path>/q.py"], ["env:DATABASE_URL", "envfile:.env"]))

    def test_no_cwd_reads_no_script(self):
        """With no cwd (an event without one, or a replay without a script body) no file is read.

        A relative path means nothing without the event's cwd; resolving it against the recorder's own
        directory would read some other file.
        """
        # GIVEN a written q.py with an env read
        q = self.cwd / "q.py"
        q.write_text("u = os.environ['DATABASE_URL']\n")
        # WHEN the command's fields are computed with no cwd
        bare = record.fields(f"python3 {q}", [], (), ())
        # THEN nothing from the script
        self.assertEqual(bare["effects"], [])

    def test_relative_and_home_paths_resolve_against_cwd_and_home(self):
        """A relative script path resolves against the event's cwd; `~/x.py` against HOME (under the cwd here).

        Agents run `python3 q.py` from the dir they wrote it in; resolving against the recorder's own cwd
        would read the wrong file or none.
        """
        # GIVEN a.py in cwd and b.py under a HOME that sits inside the cwd
        (self.cwd / "a.py").write_text("os.environ['ALPHA_KEY']\n")
        (self.cwd / "home").mkdir()
        (self.cwd / "home/b.py").write_text("os.environ['BETA_KEY']\n")
        set_env(self, HOME=str(self.cwd / "home"))
        # WHEN read
        # THEN both scripts' reads
        self.assertEqual(self.fx("python3 a.py && python3 ~/b.py", cwd=str(self.cwd)),
                         ["env:ALPHA_KEY", "env:BETA_KEY"])

    def test_a_script_in_a_temp_dir_outside_cwd_is_read(self):
        """A script under the temp dir ($TMPDIR, or /tmp) is read though it sits outside the session's cwd.

        Agents write throwaway scripts to /tmp/q.py and run them from the repo; refusing the temp dir would
        miss the most common written-then-run chore.
        """
        # GIVEN a script in a temp dir that is not under the cwd
        other = self.tmp / "other"
        other.mkdir()
        (other / "q.py").write_text("os.environ['TEMP_KEY']\n")
        # WHEN run from the cwd
        # THEN its read is there
        self.assertEqual(self.fx(f"python3 {other}/q.py", cwd=str(self.cwd)), ["env:TEMP_KEY"])

    def test_at_most_4_scripts_are_read_per_command(self):
        """A command running 5 scripts reads only the first 4.

        The recorder runs on every Bash call under a 5s hook timeout; a chain of many scripts must not
        multiply the read cost without bound.
        """
        # GIVEN 5 scripts, each with its own env read
        for i in range(5):
            (self.cwd / f"s{i}.py").write_text(f"os.environ['KEY_{'ABCDE'[i]}']\n")
        # WHEN one command runs all 5
        got = self.fx(" && ".join(f"python3 s{i}.py" for i in range(5)), cwd=str(self.cwd))
        # THEN only the first 4 scripts' reads
        self.assertEqual(got, ["env:KEY_A", "env:KEY_B", "env:KEY_C", "env:KEY_D"])

    def test_dollar_names_in_a_python_script_are_text(self):
        """A .py script's `"cost $TOTAL"` gives no env:TOTAL: a non-shell script is read as code.

        Reading python as shell would list every `$` in its strings as an env read, a false chore.
        """
        # GIVEN a python script with a `$TOTAL` in a string and a real env read
        (self.cwd / "c.py").write_text('print("cost $TOTAL")\nos.getenv("REAL_KEY")\n')
        # WHEN run
        # THEN only the real read
        self.assertEqual(self.fx("python3 c.py", cwd=str(self.cwd)), ["env:REAL_KEY"])

    def test_shell_script_steps_join_the_commands_steps(self):
        """`bash x.sh` adds x.sh's steps after the command's.

        A shell script is a chain of chores typed once into a file; its steps are what repeats across
        sessions.
        """
        # GIVEN x.sh that sets URI, reads the git log and calls it
        (self.cwd / "x.sh").write_text("#!/bin/bash\nset -e\nURI=https://api.example.test/x\n"
                                       "git log --oneline -3\ncurl -s \"$URI\"\n")
        # WHEN the command runs it
        got = record.fields("bash x.sh", [], (), (), cwd=str(self.cwd))
        # THEN its steps follow the command's
        self.assertEqual(got["steps"], ["bash x.sh", "set -e", "git log --oneline <n>", "curl -s <str>"])

    def test_script_steps_stop_at_32_after_the_commands_steps(self):
        """The command's own steps come first and the merged list stops at MAX_STEPS.

        A long script must not push the command's own steps out of the row, nor grow the row without bound.
        """
        # GIVEN a script of 40 distinct steps
        (self.cwd / "long.sh").write_text("".join(f"git tool{i} go\n" for i in range(40)))
        # WHEN run after a first step
        got = record.fields("git status && sh long.sh", [], (), (), cwd=str(self.cwd))["steps"]
        # THEN 32 steps, the command's first
        self.assertEqual((len(got), got[:3]), (32, ["git status", "sh long.sh", "git tool0 <arg>"]))

    def test_unreadable_scripts_give_no_extra_keys_and_no_crash(self):
        """A missing, oversize (>256 KB), undecodable, directory or FIFO script adds nothing and never hangs.

        The recorder runs on every Bash call; a bad file must cost only the script's signals, never the row
        (and a FIFO opened for reading would block the hook).
        """
        # GIVEN each kind of bad file, every one holding (or named for) an env read
        big = self.cwd / "big.py"
        big.write_text("os.environ['BIG_KEY']\n" + "#" * (256 * 1024))
        (self.cwd / "bin.py").write_bytes(b"\xff\xfe os.environ['BIN_KEY']\n")
        (self.cwd / "dir.py").mkdir()
        os.mkfifo(self.cwd / "fifo.py")
        # a regression that opens the FIFO would block forever: fail it after 10s instead of hanging the suite
        signal.signal(signal.SIGALRM, lambda *_: self.fail("reading a FIFO script blocked"))
        self.addCleanup(signal.alarm, 0)
        signal.alarm(10)
        for name in ("missing.py", "big.py", "bin.py", "dir.py", "fifo.py"):
            with self.subTest(name=name):
                # WHEN the command runs it
                got = record.fields(f"python3 {name}", [], (), (), cwd=str(self.cwd))
                # THEN the row's fields are there, with no effects
                self.assertEqual((got["shape"], got["effects"]), (f"python3 {name}", []))

    def test_script_outside_the_roots_or_behind_an_escaping_symlink_gives_no_keys(self):
        """A script whose real path is outside cwd, $TMPDIR and /tmp is not read, by path or through a symlink.

        The recorder reads files on the user's disk; only the session's own tree and temp dirs hold scripts an
        agent wrote, and a symlink must not open the rest of the disk.
        """
        # GIVEN a file outside the roots (this test file, in the checkout) that has signals if read
        outside = os.path.realpath(__file__)
        roots = [os.path.realpath(r) for r in (str(self.cwd), os.environ.get("TMPDIR") or "/tmp", "/tmp")]
        if any(outside.startswith(r + os.sep) for r in roots):
            self.skipTest("the checkout sits under a temp dir")
        self.assertNotEqual(effects(Path(outside).read_text(), code=True), [])
        (self.cwd / "link.py").symlink_to(outside)
        for cmd in (f"python3 {outside}", "python3 link.py"):
            with self.subTest(cmd=cmd):
                # WHEN the command runs it
                # THEN nothing is read
                self.assertEqual(self.fx(cmd, cwd=str(self.cwd)), [])

    def test_planted_secret_in_a_script_reaches_no_store_byte(self):
        """Through `agent-cli hook posttooluse`, a script holding a planted key and an API call stores only
        host:api.example.test: never the key, never a line of the script, and no new row field.

        The script is read on the user's machine at record time; storing any of its text would turn the
        shape store into a copy of every script an agent wrote, secrets included.
        """
        # GIVEN a project and a script in the session's cwd
        p = make_project(self.tmp / "proj")
        plant = "s3cr3t-plant-5"
        lines = ["import requests", f'API_KEY = "{plant}"', 'requests.get("https://api.example.test/v1")']
        (self.cwd / "call.py").write_text("\n".join(lines) + "\n")
        # WHEN the run is recorded through the hook verb with that cwd
        r = agent_cli(p, "hook", "posttooluse", stdin=json.dumps(event("python3 call.py", cwd=self.cwd)),
                      env={"AGENT_CLI_SHAPES_DIR": str(self.store)})
        # THEN one row with the usual keys and the host, and none of the text anywhere under the store
        self.assertEqual(r.returncode, 0, r.stderr)
        (row,) = store_rows(self.store)
        self.assertEqual(set(row), {"ts", "session", "agent", "repo", "shape", "steps", "effects", "exit", "rules",
                                    "bypassed"})
        self.assertEqual(row["effects"], ["host:api.example.test"])
        blob = b"".join(f.read_bytes() for f in self.store.rglob("*") if f.is_file())
        pieces = {plant[i:j] for i in range(len(plant)) for j in range(i + 6, len(plant) + 1)}
        self.assertEqual([x for x in sorted(pieces) + lines if x.encode() in blob], [])

    def test_script_text_of_a_shell_script_gives_its_steps_and_reads(self):
        """script_text (a replay's script body) feeds steps and effects with no cwd; a shell runs it here, so
        it is read as shell.

        Replays have no files on disk; the commands file carries the body as it stood when the command ran.
        """
        # GIVEN a shell body
        sh = "URI=x\ncurl \"$URI\" $API_TOKEN\n"
        # WHEN given as script_text to a command that runs it with bash
        got = record.fields("bash /gone/x.sh", [], (), (), script_text=sh)
        # THEN the body's steps and reads, without its own URI
        self.assertEqual((got["steps"], got["effects"]), (["bash <path>/x.sh", "curl <str> <arg>"],
                                                          ["env:API_TOKEN"]))

    def test_script_text_of_a_python_script_is_read_as_code(self):
        """A python command's script_text is read as code: `'$TOTAL'` is text, and it adds no steps.

        Reading a python body as shell would list every `$` in its strings as an env read and its lines as
        steps.
        """
        # GIVEN a python body
        py = "print('$TOTAL')\nos.environ['API_TOKEN']\n"
        # WHEN given as script_text to a command that runs it with python
        got = record.fields("python3 /gone/x.py", [], (), (), script_text=py)
        # THEN its code read only, and the command's own step
        self.assertEqual((got["steps"], got["effects"]), (["python3 <path>/x.py"], ["env:API_TOKEN"]))

    def test_script_text_after_a_sourced_env_file_takes_the_python_scripts_mode(self):
        """In `set -a; source .env; set +a; python3 q.py` the script body is q.py's: read as code, no steps.

        The env file is never a script; taking the mode from it read ~145 replayed python bodies as shell,
        giving junk steps (`import <arg>`) and false reads (env:TOTAL).
        """
        # GIVEN a python body for a command that sources .env first
        py = "import os\nprint('cost $TOTAL')\nos.environ['API_TOKEN']\n"
        # WHEN given as script_text
        got = self.fields_of("set -a; source .env; set +a; python3 q.py", script_text=py)
        # THEN the command's steps only and the code read, plus the env file the command names
        self.assertEqual((got["steps"], got["effects"]),
                         (["set -a", "source .env", "set +a", "python3 q.py"], ["env:API_TOKEN", "envfile:.env"]))

    def test_script_text_for_a_command_that_only_sources_an_env_file_adds_nothing(self):
        """A replay body for `. .env` (or `source .env`) is never read: an env file is not a script.

        An env file's body holds the secrets themselves; reading it would store its hosts and, as steps,
        any bare line in it.
        """
        for cmd in (". .env", "source .env"):
            with self.subTest(cmd=cmd):
                # GIVEN an env-file body with a host, a postgres URL and a bare token line
                body = "API_BASE=https://api.example.test\nDATABASE_URL=postgres://u@db.example.test/x\nZZtokenline\n"
                # WHEN given as script_text
                got = self.fields_of(cmd, script_text=body)
                # THEN only what the command text gives
                self.assertEqual((got["steps"], got["effects"]), ([cmd], ["envfile:.env"]))

    def test_script_text_is_capped_at_256_kb(self):
        """A replay body is read up to MAX_SCRIPT (256 KB), like the disk read.

        The two paths must agree, or a replay would count signals the live recorder never could.
        """
        # GIVEN a python body whose only read sits past 256 KB
        py = "#" * record.MAX_SCRIPT + "\nos.environ['LATE_KEY']\n"
        # WHEN given as script_text
        got = self.fields_of("python3 q.py", script_text=py)
        # THEN the read past the cap is not seen
        self.assertEqual(got["effects"], [])

    def test_disk_read_and_script_text_give_the_same_steps_and_effects(self):
        """For `bash x.sh`, `./x.sh`, `python3 q.py`, a python run after a sourced .env, inline python before
        a file name and `make`, reading the file from disk and passing its body as script_text give the same
        steps and effects (the last two run no file, so neither reads a body).

        Replays (script_text) are how the weekly report is checked against the live store; if the two paths
        disagree, a replay measures a different recorder than the one that runs.
        """
        # GIVEN a shell script and a python script in the cwd, and a .env beside them
        sh = "#!/bin/bash\nset -e\nURI=postgres://db.example.test/x\ngh pr view 12 --json state\npsql \"$URI\" $API_TOKEN\n"
        py = "import os\nprint('cost $TOTAL')\nos.environ['API_TOKEN']\nopen('.env')\n"
        (self.cwd / "x.sh").write_text(sh)
        os.chmod(self.cwd / "x.sh", 0o755)
        (self.cwd / "q.py").write_text(py)
        (self.cwd / ".env").write_text("API_BASE=https://env.example.test\n")
        for cmd, body in (("bash x.sh", sh), ("./x.sh", sh), ("python3 q.py", py),
                          ("set -a; source .env; set +a; python3 q.py", py), ("python3 -c 'x' q.py", py),
                          ("make", sh)):
            with self.subTest(cmd=cmd):
                # WHEN computed from disk and from the body
                disk, replay = self.fields_of(cmd, cwd=str(self.cwd)), self.fields_of(cmd, script_text=body)
                # THEN the same steps and effects
                self.assertEqual((disk["steps"], disk["effects"]), (replay["steps"], replay["effects"]))

    def test_a_shell_scripts_bare_lines_are_not_steps(self):
        """A shell script's line that does not read as a command (one bare word, or a mixed-case word) is no
        step; its commands with arguments are.

        A script can hold a token or a key body on a line of its own; the shape keeps a command word as a
        name, so storing such a line as a step would store the secret.
        """
        # GIVEN a script with a bare token line, a key-body line, a mixed-case word with an argument and two
        # commands
        (self.cwd / "x.sh").write_text(f"{WORD}\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC\nAbc123Xyz go\n"
                                       "git log -3\ncurl https://api.example.test/v1\n")
        # WHEN run
        got = self.fields_of("bash x.sh", cwd=str(self.cwd))
        # THEN only the command steps
        self.assertEqual(got["steps"], ["bash x.sh", "git log <n>", "curl <url:api.example.test>"])

    def test_a_shell_scripts_builtins_path_runs_and_path_commands_stay_steps(self):
        """Script lines whose first word is a shell builtin (`.`, `[`, `echo`, `cd`), a script run by path or
        a command on PATH (`curl`) are steps.

        The command-word check drops values a script holds; it must not drop the chores themselves, or a
        script's repeats vanish from the report.
        """
        # GIVEN a script of real command lines
        (self.cwd / "x.sh").write_text("curl -s https://api.example.test/x\n./deploy.sh\n. ./lib.sh\n"
                                       "[ -f x ] && echo y\ncd sub\n")
        # WHEN run
        got = self.fields_of("bash x.sh", cwd=str(self.cwd))
        # THEN every line is a step
        self.assertEqual(got["steps"], ["bash x.sh", "curl -s <url:api.example.test>", "<path>/deploy.sh",
                                        ". <arg>", "[ -f <v> <arg>", "echo <arg>", "cd <arg>"])

    def test_array_lines_case_labels_and_unknown_words_reach_no_store_byte(self):
        """Through the hook verb, a .sh whose array literal (one element a path), case label and a line starting
        with an unknown lowercase word hold planted values stores none of them; its curl line is still a step.

        The shape keeps a step's command word as written; a value in that place (an array element on its own
        line, a case label, a token followed by a word) would be stored as a chore's name.
        """
        # GIVEN a project and a script with the three planted values and one real command
        p = make_project(self.tmp / "proj")
        (self.cwd / "x.sh").write_text("KEYS=(\n  sk_planted_one two\n  wJal/K7MD/slashplantedkey go\n)\n"
                                       "case \"$1\" in\n"
                                       "  prodplanted) deploy now ;;\nesac\nlowercasesecret123 x\n"
                                       "curl https://api.example.test/v1\n")
        # WHEN `bash x.sh` is recorded through the hook verb with that cwd
        r = agent_cli(p, "hook", "posttooluse", stdin=json.dumps(event("bash x.sh", cwd=self.cwd)),
                      env={"AGENT_CLI_SHAPES_DIR": str(self.store)})
        # THEN the steps are the command's and curl's, and no planted value is anywhere under the store
        self.assertEqual(r.returncode, 0, r.stderr)
        (row,) = store_rows(self.store)
        self.assertEqual(row["steps"], ["bash x.sh", "curl <url:api.example.test>"])
        blob = b"".join(f.read_bytes() for f in self.store.rglob("*") if f.is_file())
        planted = ("sk_planted_one", "slashplantedkey", "prodplanted", "lowercasesecret123")
        self.assertEqual([x for x in planted if x.encode() in blob], [])

    def test_planted_secrets_in_a_shell_script_reach_no_store_byte(self):
        """Through `agent-cli hook posttooluse`, a .sh holding a secret assignment, a bare token line and a
        key-body line stores none of the three.

        The recorder reads the script on the user's machine; a line that reached the row as a step would
        make the shape store a copy of the secret.
        """
        # GIVEN a project and a script with the three planted values
        p = make_project(self.tmp / "proj")
        key, body = "sk-live-PLANTEDsh0123", "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCplant"
        (self.cwd / "x.sh").write_text(f"API_KEY={key}\n{WORD}\n{body}\ncurl -H \"x: $API_KEY\" "
                                       "https://api.example.test/v1\n")
        # WHEN `bash x.sh` is recorded through the hook verb with that cwd
        r = agent_cli(p, "hook", "posttooluse", stdin=json.dumps(event("bash x.sh", cwd=self.cwd)),
                      env={"AGENT_CLI_SHAPES_DIR": str(self.store)})
        # THEN the row has the script's command step, and no planted value is anywhere under the store
        self.assertEqual(r.returncode, 0, r.stderr)
        (row,) = store_rows(self.store)
        self.assertIn("curl -H <str> <url:api.example.test>", row["steps"])
        blob = b"".join(f.read_bytes() for f in self.store.rglob("*") if f.is_file())
        self.assertEqual([x for x in (key, WORD, body, body[:16], "PLANTEDsh") if x.encode() in blob], [])

    def test_a_sourced_envrc_or_extensionless_file_adds_nothing(self):
        """`source .envrc` and a shell sourcing or running an extensionless file (`source secrets`, `bash
        secrets`) add nothing from the file, read from disk or given as a replay body.

        direnv's .envrc and a bare `secrets` file hold exported credentials like an .env; a shell reads only
        .sh/.bash/.zsh files as scripts, so their hosts and db URLs never reach the row.
        """
        # GIVEN .envrc and secrets in the cwd, each exporting a postgres URL
        body = "export DATABASE_URL=postgres://bob:pw@prod-db.internal.test/app\n"
        (self.cwd / ".envrc").write_text(body)
        (self.cwd / "secrets").write_text(body)
        for cmd in ("source .envrc", ". ./.envrc", "source secrets", ". ./secrets", "bash secrets"):
            with self.subTest(cmd=cmd):
                # WHEN computed with the cwd, with the body as script_text, and with neither
                bare = self.fields_of(cmd)
                disk, replay = self.fields_of(cmd, cwd=str(self.cwd)), self.fields_of(cmd, script_text=body)
                # THEN neither read adds a step or an effect
                self.assertEqual([(x["steps"], x["effects"]) for x in (disk, replay)],
                                 [(bare["steps"], bare["effects"])] * 2)

    def test_a_sourced_envrc_reaches_no_store_byte(self):
        """Through the hook verb, `source .envrc` with a planted postgres URL stores no host, user or db signal.

        The hook verb is the live path; the file's names must not reach the store from it either.
        """
        # GIVEN a project and an .envrc exporting a postgres URL
        p = make_project(self.tmp / "proj")
        (self.cwd / ".envrc").write_text("export DATABASE_URL=postgres://bob:pw@prod-db.internal.test/app\n")
        # WHEN recorded through the hook verb with that cwd
        r = agent_cli(p, "hook", "posttooluse", stdin=json.dumps(event("source .envrc", cwd=self.cwd)),
                      env={"AGENT_CLI_SHAPES_DIR": str(self.store)})
        # THEN the row has no effects, and nothing of the file is under the store
        self.assertEqual(r.returncode, 0, r.stderr)
        (row,) = store_rows(self.store)
        self.assertEqual(row["effects"], [])
        blob = b"".join(f.read_bytes() for f in self.store.rglob("*") if f.is_file())
        self.assertEqual([x for x in ("prod-db", "bob", "postgres") if x.encode() in blob], [])

    def test_a_sourced_env_file_adds_nothing_to_the_row(self):
        """Through the hook verb, `. .env` and `source .env` with a planted .env add nothing from the file:
        no host, no db signal, no step; only `envfile:.env`, which the command text gives.

        The env file is the secrets file; reading it as a script would store its hosts and, as steps, its
        bare lines (a token, a key body) unmasked.
        """
        # GIVEN a project and a .env in the cwd with a host, a postgres URL, a bare token and a key body
        p = make_project(self.tmp / "proj")
        (self.cwd / ".env").write_text("API_BASE=https://secret-host.example.test\n"
                                       "DATABASE_URL=postgres://u:pw@db.example.test/x\n"
                                       f"{WORD}\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n")
        for cmd in (". .env", "source .env"):
            with self.subTest(cmd=cmd):
                store = self.tmp / f"store-{cmd[0]}"
                # WHEN recorded through the hook verb with that cwd
                r = agent_cli(p, "hook", "posttooluse", stdin=json.dumps(event(cmd, cwd=self.cwd)),
                              env={"AGENT_CLI_SHAPES_DIR": str(store)})
                # THEN the row holds the command's step and envfile only, and nothing of the file
                self.assertEqual(r.returncode, 0, r.stderr)
                (row,) = store_rows(store)
                self.assertEqual((row["steps"], row["effects"]), ([cmd], ["envfile:.env"]))
                blob = b"".join(f.read_bytes() for f in store.rglob("*") if f.is_file())
                self.assertEqual([x for x in ("example.test", WORD, "MIIEvQ") if x.encode() in blob], [])

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
