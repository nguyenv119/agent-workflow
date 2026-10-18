"""Tests for the repeated-shape warning of `agent-cli hook pretooluse`: the per-step cache lookup, the
most-specific pick, once per (agent, step), the per-session cap, and its delivery next to the rules."""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

import helpers
from helpers import CLAUDE, agent_cli, make_project, post_event

sys.path.insert(0, str(CLAUDE / "cli"))
from agent_cli import hook, shape  # noqa: E402


REPEAT_SEAMS = {"command-prefixes": "wrapx\nwrapx raw\n"}  # a made-up wrapper word, never a real one
CHORE = "deploy-tool push --env staging"  # typed as PreToolUse sees it; the recorder saw it wrapped
CHORE_SHAPE = "deploy-tool <arg> --env <v>"


class RepeatCase(helpers.HookCase):
    """A project with the wrapper seam and a shape store in a temp dir, seeded through the real hook verb."""

    def setUp(self):
        super().setUp()
        self.p = make_project(self.tmp / "rep", REPEAT_SEAMS)
        self.bin = str(self.p / ".claude/bin")
        self.store = self.tmp / "shapes"
        self.env = {"AGENT_CLI_SHAPES_DIR": str(self.store)}

    def seed(self, command, sessions=3, project=None):
        """Record command from `sessions` distinct sessions via `hook posttooluse`, then write the cache."""
        for i in range(sessions):
            r = agent_cli(project or self.p, "hook", "posttooluse", env=self.env,
                          stdin=json.dumps(post_event(command, session=f"seed{i}", cwd=None)))
            self.assertEqual(r.returncode, 0, r.stderr)
        r = agent_cli(project or self.p, "repeats", "--since", "14d", "--write-cache", env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)

    def write_cache(self, cache):
        self.store.mkdir(exist_ok=True)
        (self.store / "repeats-cache.json").write_text(cache if isinstance(cache, str) else json.dumps(cache))

    def ask(self, command, session="live1", agent=None, env=None, project=None):
        """The pretooluse hook's decision for command typed in session (and agent)."""
        event = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": command}}
        if session is not None:
            event["session_id"] = session
        if agent:
            event["agent_id"] = agent
        p = agent_cli(project or self.p, "hook", "pretooluse", stdin=json.dumps(event),
                      env={"PATH": self.bin + os.pathsep + os.environ["PATH"], **self.env, **(env or {})})
        return self.decision(p)


class RepeatWarning(RepeatCase):
    def test_cached_shape_warns_once_per_session_then_is_silent(self):
        """A shape on the repeats cache gets the warning (counts, the skill, the bypass) once per session.

        The recorder saw the command after the wrapper rewrite and PreToolUse sees it as typed; the seam
        strips the wrapper, so both give one shape. Once per session: a warning on every run is noise
        the agent learns to skip.
        """
        # GIVEN the chore recorded wrapped in 3 sessions and the cache written
        self.seed("wrapx " + CHORE)
        # WHEN a new session types it unwrapped twice, and another session types it once
        first = self.ask(CHORE)
        again = self.ask("deploy-tool push --env prod")
        other = self.ask(CHORE, session="live2")
        # THEN the first run of each session warns, with no decision; the repeat in a session is silent
        self.assertNotIn("permissionDecision", first)
        text = first["additionalContext"]
        self.assertIn(f"`{CHORE_SHAPE}` was hand-written 3 times across 3 sessions", text)
        self.assertIn("propose a command (cli-for-agents skill", text)
        self.assertIn(".claude/skills/cli-for-agents/SKILL.md", text)
        self.assertIn("(bypass: append  # agent-cli: allow repeated-shape)", text)
        self.assertIsNone(again)
        self.assertEqual(other["additionalContext"], text)

    def test_a_subagent_gets_its_own_warning(self):
        """A subagent in the same session (another agent_id) is warned again; it never saw the parent's."""
        # GIVEN a cached chore, already warned to the main agent of session live1
        self.seed(CHORE)
        self.assertIsNotNone(self.ask(CHORE))
        # WHEN a subagent of that session types it, twice
        sub = self.ask(CHORE, agent="agent-7")
        sub_again = self.ask(CHORE, agent="agent-7")
        # THEN it is warned once
        self.assertIn("hand-written 3 times", sub["additionalContext"])
        self.assertIsNone(sub_again)

    def test_uncached_shape_is_silent(self):
        """A shape the cache does not hold gets nothing: the warning is for repeats, not every command."""
        # GIVEN a cache holding only the chore
        self.seed(CHORE)
        # WHEN another command is typed
        # THEN nothing is printed
        self.assertIsNone(self.ask("deploy-tool push --tag v1"))

    def test_a_shape_in_two_sessions_is_not_cached_and_stays_silent(self):
        """Control on the seed: below 3 sessions the shape is not a repeat, so the same command is silent."""
        # GIVEN the chore recorded in only 2 sessions
        self.seed(CHORE, sessions=2)
        # WHEN it is typed
        # THEN nothing is printed
        self.assertIsNone(self.ask(CHORE))

    def test_denied_command_shows_the_deny_not_the_repeat_warning(self):
        """When a rule denies, the reason is the rule's message only, and the session is not marked.

        The deny already names the command to use; a second message would compete with it.
        """
        # GIVEN a cache that (by hand) holds the shape of a command the built-in worktree rule denies
        self.write_cache({"git worktree <arg> -b <v> <path>": {"sessions": 5, "count": 9}})
        # WHEN the denied command is typed
        out = self.ask("git worktree add -b x ../x")
        # THEN it is denied with the rule's message, no repeat text, and nothing marked as warned
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertIn("agent-cli wt new", out["permissionDecisionReason"])
        self.assertNotIn("hand-written", json.dumps(out))
        self.assertFalse((self.store / "warned").exists())

    def test_a_firing_warn_rule_and_the_repeat_warning_are_both_delivered(self):
        """When a warn rule fires on a command with a cached step, the rule's message comes first and the
        repeated-shape warning follows it.

        The pointer rules fire on many chains (any `sleep 20+`); if a firing rule silenced the repeat
        warning, every other step of those chains would never be warned about.
        """
        # GIVEN a project warn rule on the chore, and the chore cached by hand
        rule = [{"id": "deploy-hint", "match": "{CMD}deploy-tool\\b", "mode": "warn", "message": "use ship"}]
        p = make_project(self.tmp / "warn", dict(REPEAT_SEAMS, **{"pretooluse-rules.json": json.dumps(rule)}))
        self.write_cache({CHORE_SHAPE: {"sessions": 4, "count": 6}})
        # WHEN the chore is typed there
        ruled = self.ask(CHORE, project=p)
        # THEN the rule line comes first, then the repeat warning
        lines = ruled["additionalContext"].splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("use ship", lines[0])
        self.assertIn("hand-written 6 times across 4 sessions", lines[1])

    def test_a_deny_in_a_chain_silences_the_warning_for_its_other_steps(self):
        """When a rule denies one step of a chain, the reason is the deny alone, even though another step of
        that chain is cached, and the session is not marked.

        The command does not run, so a warning about its other step would be noise next to the deny.
        """
        # GIVEN the chore cached
        self.write_cache({CHORE_SHAPE: {"sessions": 4, "count": 6}})
        # WHEN a chain of a denied worktree add and the chore is typed
        out = self.ask("git worktree add -b x ../x && " + CHORE)
        # THEN it is denied with no repeat text, and nothing is marked as warned
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertNotIn("hand-written", json.dumps(out))
        self.assertFalse((self.store / "warned").exists())

    def test_a_shape_this_project_covers_by_a_command_is_silent(self):
        """A cached shape whose command is on this project's `commands:` line (or agent-cli) never warns.

        The store and its cache are shared by every project, and the cache was filtered with the
        `commands:` line of whichever project's session started last; another project's list must not
        tell this project to wrap a command it already has.
        """
        # GIVEN a cache (written elsewhere) holding a shape of this project's own CLI and one of agent-cli
        own = make_project(self.tmp / "own", REPEAT_SEAMS)
        (own / ".claude/project-rules.md").write_text("## repeats\ncommands: deploy-tool\n")
        self.write_cache({CHORE_SHAPE: {"sessions": 4, "count": 6},
                          "agent-cli wt <arg>": {"sessions": 4, "count": 6}})
        # WHEN both are typed in that project, and the chore in one without the line (control)
        own_cli = self.ask(CHORE, project=own)
        agent = self.ask("wrapx agent-cli wt list", project=own)
        control = self.ask(CHORE)
        # THEN the covered ones are silent and the control warns
        self.assertEqual((own_cli, agent), (None, None))
        self.assertIn("hand-written 6 times", control["additionalContext"])

    def test_a_bad_cache_is_silent_and_rule_warnings_still_show(self):
        """A missing, unparseable or wrong-typed cache gives no repeat warning (the plain project), and a rule's
        warning is unaffected by it (the ruled project, where the rule fires and the cache is read too).

        The cache is written by a background SessionStart run that can be killed mid-way on an old
        install; a broken cache must cost nothing.
        """
        rule = [{"id": "deploy-hint", "match": "{CMD}deploy-tool\\b", "mode": "warn", "message": "use ship"}]
        p = make_project(self.tmp / "bad", dict(REPEAT_SEAMS, **{"pretooluse-rules.json": json.dumps(rule)}))
        for label, cache in (("missing", None), ("not json", "{trunc"), ("a list", "[]"),
                             ("no counts", json.dumps({CHORE_SHAPE: {}})),
                             ("string counts", json.dumps({CHORE_SHAPE: "x"}))):
            with self.subTest(cache=label):
                # GIVEN that cache
                shutil.rmtree(self.store, ignore_errors=True)
                if cache is not None:
                    self.write_cache(cache)
                # WHEN the chore is typed (as plain, and with the warn rule's project)
                plain = self.ask(CHORE, session=f"bad-{len(label)}")
                ruled = self.ask(CHORE, session=f"bad-r{len(label)}", project=p)
                # THEN no repeat warning, and the rule's warning alone
                self.assertIsNone(plain)
                self.assertEqual(ruled["additionalContext"].splitlines(), [ruled["additionalContext"]])
                self.assertIn("use ship", ruled["additionalContext"])

    def test_bypass_comment_kill_switch_and_missing_session_are_silent(self):
        """`# agent-cli: allow repeated-shape` and AGENT_CLI_HOOKS=off silence it; with no session id there
        is no way to warn only once, so it stays silent rather than warn on every run."""
        # GIVEN a cached chore
        self.seed(CHORE)
        # WHEN it is typed with the bypass, with the kill switch, and with no session id
        bypassed = self.ask(CHORE + "  # agent-cli: allow repeated-shape", session="b1")
        off = self.ask(CHORE, session="b2", env={"AGENT_CLI_HOOKS": "off"})
        no_session = self.ask(CHORE, session=None)
        # THEN all three print nothing, and the control in a fresh session still warns
        self.assertEqual((bypassed, off, no_session), (None, None, None))
        self.assertIsNotNone(self.ask(CHORE, session="b3"))

    def test_an_unwritable_store_is_silent(self):
        """When the warned-once mark cannot be written the hook says nothing, rather than warn every run."""
        # GIVEN a cached chore in a store dir that refuses writes
        self.seed(CHORE)
        self.store.chmod(0o500)
        self.addCleanup(self.store.chmod, 0o700)
        # WHEN it is typed
        # THEN nothing is printed
        self.assertIsNone(self.ask(CHORE))

    def test_session_id_cannot_write_outside_the_warned_dir(self):
        """A session id with `/` or `..` is flattened to one file name inside <store>/warned/."""
        # GIVEN a cached chore
        self.seed(CHORE)
        # WHEN a session id that is a path is used
        self.assertIsNotNone(self.ask(CHORE, session="../../escape"))
        # THEN exactly one file exists, directly inside warned/
        files = [f for f in self.tmp.rglob("*") if f.is_file() and "escape" in f.name]
        self.assertEqual([f.parent for f in files], [self.store / "warned"])

    def test_five_thousand_shape_cache_stays_well_inside_the_timeout(self):
        """With a 5,000-shape cache the hook answers in under 5 s (its settings timeout), warning included.

        A hook past its timeout is killed; on every Bash call that would add seconds and lose the warning.
        """
        # GIVEN a cache of 5,000 shapes plus the chore
        cache = {f"tool{i} <arg> --flag{i % 50} <v>": {"sessions": 3 + i % 7, "count": 3 + i} for i in range(5000)}
        cache[CHORE_SHAPE] = {"sessions": 3, "count": 3}
        self.write_cache(cache)
        # WHEN the chore is typed
        t0 = time.monotonic()
        out = self.ask(CHORE)
        took = time.monotonic() - t0
        # THEN it warns, inside the timeout
        sys.stderr.write(f"\npretooluse with a 5,000-shape cache: {took:.2f}s\n")
        self.assertIn("hand-written 3 times", out["additionalContext"])
        self.assertLess(took, 5)


class QuietShapes(RepeatCase):
    def test_generic_read_shapes_never_warn(self):
        """Shapes built only from generic reads (cat, sed -n, grep, head, git status/diff, inline python)
        never warn, even when cached: they top the repeats list but carry no chore a command could wrap."""
        quiet = {"sed -n <n> <path>": "sed -n 1,20p src/a.py", "grep -n <str> <path> | head <n>": "grep -n 'x' src/a.py | head -5",
                 "python3 - <heredoc>": "python3 - <<'EOF'\nprint(1)\nEOF", "git status": "git status",
                 "python3 -c <str>; echo <arg>": "python3 -c 'print(1)'; echo done",
                 "git diff <path> -- <path>": "git diff a/b -- c/d"}
        loud = {"python3 <path>/report.py --day <n>": "python3 tools/report.py --day 3",
                "cat <path> | deploy-tool <arg>": "cat a/b | deploy-tool push"}
        # GIVEN every step of those shapes cached (the cache is keyed by step)
        self.write_cache({st: {"sessions": 9, "count": 40} for s in {**quiet, **loud} for st in shape.steps_of_shape(s)})
        for i, (s, cmd) in enumerate({**quiet, **loud}.items()):
            with self.subTest(shape=s):
                # WHEN its command is typed (the test checks the hook shaped it as cached)
                self.assertEqual(shape.shape(cmd, (), ()), s)
                out = self.ask(cmd, session=f"q{i}")
                # THEN reads are silent and a pipeline with a real tool, or a named script, warns
                if s in quiet:
                    self.assertIsNone(out)
                else:
                    self.assertIn("hand-written 40 times", out["additionalContext"])


class StepLookup(RepeatCase):
    """The warning looks up each step of the typed command in the step-keyed cache and names one step."""

    def test_a_one_step_command_warns_on_its_step_key(self):
        """A one-step command whose path folds to <arg> warns with the step the cache is keyed by.

        The cache is keyed by step, and steps fold <path> into <arg>; looking up the whole shape
        (`deploy-tool <arg> <path>`) would miss and the warning would go quiet for most chores.
        """
        # GIVEN the cache holding the step `deploy-tool <arg>`, and nothing under the whole shape
        self.write_cache({"deploy-tool <arg>": {"sessions": 4, "count": 7}})
        # WHEN a command whose shape is `deploy-tool <arg> <path>` is typed, twice
        first = self.ask("deploy-tool push ./dist")
        again = self.ask("deploy-tool push ./out")
        # THEN it warns once, naming the step
        self.assertEqual(shape.shape("deploy-tool push ./dist", (), ()), "deploy-tool <arg> <path>")
        self.assertIn("`deploy-tool <arg>` was hand-written 7 times across 4 sessions", first["additionalContext"])
        self.assertIsNone(again)

    def test_a_substitution_body_step_warns_like_the_recorder_split_it(self):
        """A command whose only step is a $( ) body (`X=$(deploy-tool push --env staging)`) warns on that
        body's step, the key the recorder stored it under (a made-up tool: a built-in rule fires on
        `gh pr view` and would add its own line).

        The recorder splits commands with shape.steps(), $( ) bodies included; a warning that split the
        whole shape instead would see only the assignment and never warn on a chore typed this way.
        """
        # GIVEN the body's step cached
        self.write_cache({CHORE_SHAPE: {"sessions": 5, "count": 8}})
        # WHEN the assignment is typed
        out = self.ask(f"X=$({CHORE})")
        # THEN it warns, naming the body's step
        self.assertIn(f"`{CHORE_SHAPE}` was hand-written 8 times", out["additionalContext"])

    def test_a_chain_warns_about_its_most_specific_cached_step(self):
        """In `cd app && . .env && npm run db:query 'select 1'` with both steps cached, the warning names
        `npm run db:query <str>` (14 literal characters beat 5), once, marked as one step of the command; the
        same step typed alone later in that session is silent.

        One warning per command keeps it readable; the step with the most literal text is the one a command
        could wrap, while `. .env` is setup. In the 17 Oct test every seeded chore sat inside a chain, so a
        whole-command lookup never fired.
        """
        # GIVEN both steps cached, the less specific one in more sessions
        self.write_cache({". .env": {"sessions": 9, "count": 30}, "npm run db:query <str>": {"sessions": 6, "count": 22}})
        # WHEN the chain is typed, then the query step alone in the same session
        self.assertEqual(shape.steps("cd app && . .env && npm run db:query 'select 1'", (), ()), [". .env", "npm run db:query <str>"])
        first = self.ask("cd app && . .env && npm run db:query 'select 1'")
        alone = self.ask("npm run db:query 'select 2'")
        # THEN one line, naming the query step with the one-step note; the lone repeat is silent
        self.assertEqual(first["additionalContext"].splitlines(), [first["additionalContext"]])
        self.assertIn("`npm run db:query <str>` was hand-written 22 times across 6 sessions (one step of this command); "
                      "propose a command", first["additionalContext"])
        self.assertNotIn(". .env", first["additionalContext"])
        self.assertIsNone(alone)

    def test_a_one_step_command_has_no_one_step_note(self):
        """A command of one step gets the warning without `(one step of this command)`: the note is there only
        to tell the agent which part of a chain is meant."""
        # GIVEN the chore cached
        self.write_cache({CHORE_SHAPE: {"sessions": 4, "count": 6}})
        # WHEN it is typed alone
        out = self.ask(CHORE)
        # THEN the warning has no one-step note
        self.assertIn("across 4 sessions; propose a command", out["additionalContext"])
        self.assertNotIn("one step of this command", out["additionalContext"])

    def test_an_already_warned_step_lets_the_next_cached_step_of_a_chain_warn(self):
        """Typing the same chain again in the session warns about its other cached step: a step already
        warned about is no longer a candidate, so it cannot hide a different step of the same command."""
        # GIVEN both steps cached, and the chain already warned once (about the query step)
        self.write_cache({". .env": {"sessions": 9, "count": 30}, "npm run db:query <str>": {"sessions": 6, "count": 22}})
        self.assertIn("npm run db:query", self.ask("cd app && . .env && npm run db:query 'select 1'")["additionalContext"])
        # WHEN the chain is typed again, and a third time
        second = self.ask("cd app && . .env && npm run db:query 'select 2'")
        third = self.ask("cd app && . .env && npm run db:query 'select 3'")
        # THEN the second names `. .env`, and the third is silent (both steps warned)
        self.assertIn("`. .env` was hand-written 30 times across 9 sessions (one step of this command)",
                      second["additionalContext"])
        self.assertIsNone(third)

    def test_equal_literal_text_is_broken_by_more_sessions(self):
        """Two cached steps with the same number of literal characters: the one seen in more sessions is
        named, even when it comes second in the command (more sessions = more widely repeated)."""
        # GIVEN two 6-literal-character steps cached, the second in more sessions
        self.write_cache({"tool-a <arg>": {"sessions": 4, "count": 50}, "tool-b <arg>": {"sessions": 7, "count": 9}})
        # WHEN both are typed in one chain
        out = self.ask("tool-a x1 && tool-b x2")
        # THEN tool-b is named
        self.assertIn("`tool-b <arg>` was hand-written 9 times across 7 sessions", out["additionalContext"])

    def test_a_full_tie_names_the_first_step_in_command_order(self):
        """Two cached steps equal in literal characters and sessions: the first one typed is named, so the
        pick never depends on the cache's key order."""
        # GIVEN two steps tied on literal characters and sessions, the cache listing the second first
        self.write_cache({"tool-b <arg>": {"sessions": 5, "count": 9}, "tool-a <arg>": {"sessions": 5, "count": 8}})
        # WHEN tool-a is typed before tool-b
        out = self.ask("tool-a x1; tool-b x2")
        # THEN tool-a is named
        self.assertIn("`tool-a <arg>` was hand-written 8 times", out["additionalContext"])

    def test_literal_characters_beat_a_longer_placeholder_heavy_step(self):
        """A short, mostly literal step is named over a longer step that is mostly placeholders: specificity is
        the literal text (16 characters against 6), not the step's length (27 against 36).

        A placeholder-heavy step (`tool-a <arg> <str> ...`) matches almost any call of that tool, so naming it
        would point the agent at the vaguest repeat in the chain.
        """
        # GIVEN both steps cached, the placeholder-heavy one first and in more sessions
        cmd = "tool-a one 'two words' three 'four' five && " + CHORE
        heavy = "tool-a <arg> <str> <arg> <str> <arg>"
        self.assertEqual(shape.steps(cmd, (), ()), [heavy, CHORE_SHAPE])
        self.write_cache({heavy: {"sessions": 9, "count": 50}, CHORE_SHAPE: {"sessions": 4, "count": 6}})
        # WHEN the chain is typed
        out = self.ask(cmd)
        # THEN the chore is named
        self.assertIn(f"`{CHORE_SHAPE}` was hand-written 6 times", out["additionalContext"])

    def test_spaces_do_not_count_as_literal_characters(self):
        """One long word (18 literal characters) is named over many short flags (16 literal characters plus 7
        spaces): spaces between words are not literal text, so a step is not more specific for having more
        words."""
        # GIVEN both steps cached, the many-flag one first and in more sessions
        cmd = "mk -a -b -c -d -e -f -g; deploy-toolkit-xyz"
        self.assertEqual(shape.steps(cmd, (), ()), ["mk -a -b -c -d -e -f -g", "deploy-toolkit-xyz"])
        self.write_cache({"mk -a -b -c -d -e -f -g": {"sessions": 9, "count": 50},
                          "deploy-toolkit-xyz": {"sessions": 4, "count": 6}})
        # WHEN the chain is typed
        out = self.ask(cmd)
        # THEN the long word is named
        self.assertIn("`deploy-toolkit-xyz` was hand-written 6 times", out["additionalContext"])

    def test_a_session_gets_at_most_five_warnings_across_its_agents(self):
        """After 5 repeated-shape warnings in one session (main agent and subagents together), a 6th distinct
        step is silent for every agent and nothing more is written; another session still warns.

        Once the cache holds hundreds of steps, an uncapped warning would fire on most commands and be
        skipped like noise; 5 is what a person reading the session sees.
        """
        # GIVEN six distinct cached steps
        self.write_cache({f"tool{i} <arg>": {"sessions": 4, "count": 10 + i} for i in range(6)})
        # WHEN the main agent hits three, a subagent two, then the subagent and the main agent the sixth
        warned = [self.ask(f"tool{i} x") for i in range(3)] + [self.ask(f"tool{i} x", agent="sub-1") for i in (3, 4)]
        sixth = (self.ask("tool5 x", agent="sub-1"), self.ask("tool5 x"))
        other = self.ask("tool5 x", session="live2")
        # THEN the first five warn, the sixth is silent for both, the mark file holds five lines, and the
        # other session warns
        self.assertTrue(all(w and "hand-written" in w["additionalContext"] for w in warned))
        self.assertEqual(sixth, (None, None))
        self.assertEqual(len((self.store / "warned" / "live1.txt").read_text().splitlines()), 5)
        self.assertIn("`tool5 <arg>` was hand-written 15 times", other["additionalContext"])

    def test_a_chain_of_only_basic_steps_never_warns_even_when_cached(self):
        """A chain made only of shell basics (mkdir, cd, git status, sleep) never warns, even when a stale or
        hand-made cache holds every step: there is no chore in it for a command to wrap."""
        # GIVEN every step of the chain cached
        cmd = "mkdir -p out && cd out && git status; sleep 5"
        self.write_cache({s: {"sessions": 9, "count": 40} for s in shape.steps(cmd, (), ())})
        # WHEN it is typed
        # THEN nothing is printed
        self.assertTrue(shape.steps(cmd, (), ()))
        self.assertIsNone(self.ask(cmd))

    def test_a_covered_step_is_skipped_for_the_next_cached_step(self):
        """A step whose command this project already has (agent-cli) is never named, even when it holds the
        most literal text; the chain's other cached step is named instead."""
        # GIVEN an agent-cli step (21 literal characters) and the chore (16) cached
        self.write_cache({"agent-cli wt list --json": {"sessions": 9, "count": 40},
                          CHORE_SHAPE: {"sessions": 4, "count": 6}})
        # WHEN both are typed in one chain
        out = self.ask("agent-cli wt list --json; " + CHORE)
        # THEN the chore is named
        self.assertIn(f"`{CHORE_SHAPE}` was hand-written 6 times", out["additionalContext"])
        self.assertNotIn("agent-cli wt list --json`", out["additionalContext"])

    def test_masked_chains_from_the_warning_test_warn_on_their_chore_step(self):
        """The two chain kinds the 17 Oct warning test seeded (`<arg>=<v>; gh pr view <n> --json <v> | jq <str>;
        sleep <n>` and `cd <arg> && git -C <arg> worktree list; agent-cli wt list`) warn about the chore step,
        after the built-in pointer rule's line.

        Both were silent under the whole-command lookup, which is why that test saw no warnings.
        """
        cases = {"PR=12; gh pr view 12 --json state | jq '.state'; sleep 30": "gh pr view <n> --json <v> | jq <str>",
                 "cd ../wt && git -C ../wt worktree list; agent-cli wt list": "git -C <arg> worktree list"}
        for i, (cmd, step) in enumerate(cases.items()):
            with self.subTest(cmd=cmd):
                # GIVEN every step of the chain cached
                steps = shape.steps(cmd, (), ())
                self.assertIn(step, steps)
                self.write_cache({s: {"sessions": 5, "count": 12} for s in steps})
                # WHEN it is typed
                out = self.ask(cmd, session=f"m{i}")
                # THEN the last line names the chore step as one step of the command
                self.assertIn(f"`{step}` was hand-written 12 times across 5 sessions (one step of this command)",
                              out["additionalContext"].splitlines()[-1])

    def test_a_basic_step_never_warns_even_when_cached(self):
        """A one-step command that is basic (inline interpreter code after a flag, git add, mkdir) never
        warns, even when a cache lists it.

        A whole-shape read check once let `python3 -I - <heredoc>` through, and a stale or hand-made cache can still hold such
        a step; the warning must not tell agents to wrap a shell basic in a command.
        """
        cmds = {"python3 -I - <heredoc>": "python3 -I - <<'EOF'\nprint(1)\nEOF", "git -C <arg> add <arg>": "git -C ../wt add a.txt",
                "mkdir -p <arg>": "mkdir -p out/x"}
        # GIVEN every one of those steps cached
        self.write_cache({s: {"sessions": 9, "count": 40} for s in cmds})
        for i, (s, cmd) in enumerate(cmds.items()):
            with self.subTest(step=s):
                # WHEN its command is typed (the test checks it is that one step)
                self.assertEqual(shape.steps(cmd, (), ()), [s])
                # THEN nothing is printed
                self.assertIsNone(self.ask(cmd, session=f"b{i}"))


class ChoreLookup(RepeatCase):
    """A step piped into quiet reads (`| head`, `| grep`) is looked up, marked and named as its first segment."""

    def test_a_quiet_tail_variant_warns_naming_the_chore(self):
        """With `bd show <arg>` on the cache (recorded as `bd show x | head -20` in 3 sessions), typing
        `bd show y | head -5` warns naming `bd show <arg>`, and `bd show z | grep open` in the same session and
        agent is then silent.

        Keyed by the whole pipeline, each quiet tail was a new step with its own warning, so one chore spent
        the 5-per-session cap on its variants.
        """
        # GIVEN the chore recorded with a quiet tail in 3 sessions, and the cache written
        self.seed("bd show x-1 | head -20")
        self.assertEqual(set(json.loads((self.store / "repeats-cache.json").read_text())), {"bd show <arg>"})
        # WHEN a head variant, then a grep variant, are typed in one session
        first = self.ask("bd show x-2 | head -5")
        grep = self.ask("bd show x-3 | grep open")
        # THEN the first warns naming the chore, and the grep variant is silent
        self.assertIn("`bd show <arg>` was hand-written 3 times across 3 sessions; propose a command",
                      first["additionalContext"])
        self.assertIsNone(grep)

    def test_another_agent_gets_the_chore_warning_once(self):
        """After the main agent was warned about `bd show <arg>`, a subagent of the same session typing a tail
        variant is warned once, and its next variant is silent: once per (agent, chore)."""
        # GIVEN the chore cached and already warned to the main agent
        self.write_cache({"bd show <arg>": {"sessions": 4, "count": 9}})
        self.assertIsNotNone(self.ask("bd show x-1"))
        # WHEN a subagent types two tail variants
        sub = self.ask("bd show x-2 | grep -n open | head -3", agent="agent-7")
        sub_again = self.ask("bd show x-3 | tail -2", agent="agent-7")
        # THEN it is warned once, naming the chore
        self.assertIn("`bd show <arg>` was hand-written 9 times", sub["additionalContext"])
        self.assertIsNone(sub_again)

    def test_a_recipe_tail_is_not_folded(self):
        """A step piped into a recipe (`| tr`) keeps its whole pipeline as the key: with only the first segment
        cached it is silent, and with the whole pipeline cached it warns naming the pipeline."""
        # GIVEN only the first segment cached
        self.write_cache({"x-tool <arg>": {"sessions": 4, "count": 9}})
        # WHEN the recipe pipeline is typed
        # THEN nothing is printed
        self.assertIsNone(self.ask("x-tool run | tr a b"))
        # GIVEN the whole pipeline cached, WHEN it is typed in a new session, THEN the pipeline is named
        self.write_cache({"x-tool <arg> | tr <arg>": {"sessions": 4, "count": 9}})
        self.assertIn("`x-tool <arg> | tr <arg>` was hand-written 9 times",
                      self.ask("x-tool run | tr a b", session="live2")["additionalContext"])

    def test_two_variants_of_one_chore_in_one_command_warn_once_with_one_mark(self):
        """A chain holding two tail variants of one chore (`bd show x | head -5; bd show y | grep open`) gives one
        warning, about the chore, and writes exactly one mark: the variants share one key, so they never spend
        two of the session's 5 warnings."""
        # GIVEN the chore cached
        self.write_cache({"bd show <arg>": {"sessions": 4, "count": 9}})
        # WHEN one command holds two of its variants
        out = self.ask("bd show x-1 | head -5; bd show x-2 | grep open")
        # THEN one warning names the chore, and the session's marks file holds one line
        self.assertIn("`bd show <arg>` was hand-written 9 times across 4 sessions (one step of this command)",
                      out["additionalContext"])
        marks = (self.store / "warned" / "live1.txt").read_text().splitlines()
        self.assertEqual(marks, [json.dumps(["", "bd show <arg>"])])

    def test_the_cap_stays_five_with_tail_variants(self):
        """Six distinct chores each typed with a quiet tail still give at most 5 warnings in a session."""
        # GIVEN six cached chores
        self.write_cache({f"tool{i} <arg>": {"sessions": 4, "count": 10 + i} for i in range(6)})
        # WHEN each is typed piped into head
        out = [self.ask(f"tool{i} x | head -3") for i in range(6)]
        # THEN the first five warn, each naming its chore, and the sixth is silent
        self.assertEqual([bool(o) for o in out], [True] * 5 + [False])
        self.assertIn("`tool4 <arg>` was hand-written 14 times", out[4]["additionalContext"])


class StepSpeed(unittest.TestCase):
    def test_a_20kb_chain_splits_and_looks_up_in_under_50ms(self):
        """shape.steps() on a ~20 KB realistic chain, and the whole warning lookup on it, each take under
        50 ms (best of 3, so a loaded machine does not fail it).

        The hook has a 5 s budget shared with the rules and runs before every Bash call; a slow split would
        add delay to every command and, past the budget, lose the warning.
        """
        # GIVEN a ~20 KB chain of cd, git, gh | jq, $( ) bodies, a script with redirects, and sleeps
        parts, i = [], 0
        while len("; ".join(parts)) < 20 * 1024 - 300:
            parts.append(f"cd /repo/wt{i} && git -C /repo/wt{i} status --short | head -{i % 9 + 1}; "
                         f"gh pr view {1000 + i} --json state | jq '.state'; X=$(grep KEY{i} .env | cut -d= -f2) "
                         f"&& python3 tools/report{i % 7}.py --day {i % 30} > out{i}.txt 2>&1 && sleep {i % 60}")
            i += 1
        cmd = "; ".join(parts)
        store = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, store, ignore_errors=True)
        helpers.set_env(self, AGENT_CLI_SHAPES_DIR=str(store), AGENT_CLI_HOOKS=None)
        cache = {"python3 <path>/report0.py --day <n>": {"sessions": 4, "count": 9}}
        (store / "repeats-cache.json").write_text(json.dumps(cache))
        # WHEN it is split, and looked up, three times each
        split, lookup = [], []
        for n in range(3):
            t0 = time.perf_counter()
            shape.steps(cmd, (), ())
            split.append(time.perf_counter() - t0)
            t0 = time.perf_counter()
            hit = hook._repeat_rule(cmd, {"session_id": f"speed{n}"})
            lookup.append(time.perf_counter() - t0)
        # THEN the best of each is under 50 ms (and the lookup did warn, so it ran to the end)
        self.assertGreater(len(cmd), 20 * 1024 - 400)
        self.assertIn("python3 <path>/report0.py --day <n>", hit["message"])
        self.assertLess(min(split), 0.05)
        self.assertLess(min(lookup), 0.05)


if __name__ == "__main__":
    unittest.main()
