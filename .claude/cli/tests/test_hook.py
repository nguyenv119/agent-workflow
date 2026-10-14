"""Tests for the PreToolUse hook framework: `agent-cli hook pretooluse|test|rules`, run as a real subprocess."""
import json
import os
import re
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

from helpers import CLAUDE, CORPUS, agent_cli, bash_event, corpus_entries, make_project, post_event

sys.path.insert(0, str(CLAUDE / "cli"))
from agent_cli import hook, hook_rules, shape  # noqa: E402

VERDICT = "agent-cli-hand-verdict"
WT_RULE = json.dumps([{"id": "test-wt", "match": r"{CMD}git\s+worktree\s+add\b", "mode": "deny",
                       "message": "use the worktree command"}])


class HookCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp(prefix="agent-cli-hook-")))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.p = make_project(self.tmp / "proj")
        self.bin = str(self.p / ".claude/bin")

    def hook(self, command, tool="Bash", env=None, project=None):
        return agent_cli(project or self.p, "hook", "pretooluse", stdin=bash_event(command, tool),
                         env={"PATH": self.bin + os.pathsep + os.environ["PATH"], **(env or {})})

    def decision(self, p):
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)["hookSpecificOutput"] if p.stdout.strip() else None


class Deny(HookCase):
    def test_hand_printed_sentinel_is_denied_with_the_envelope(self):
        """A worker echoing the WIN sentinel gets Claude Code's deny envelope, naming the bypass.

        The sentinel is the eval's verdict; a worker printing it fakes a WIN the loop then trusts.
        """
        # GIVEN the canonical hand-printed sentinel
        # WHEN the hook reads it
        out = self.decision(self.hook('echo "<promise>WIN</promise>"'))
        # THEN it is denied with the rule's message and bypass hint
        self.assertEqual(out["hookEventName"], "PreToolUse")
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertIn("The eval prints WIN, not a worker.", out["permissionDecisionReason"])
        self.assertIn("(bypass: append  # agent-cli: allow agent-cli-hand-verdict)", out["permissionDecisionReason"])

    def test_every_must_deny_form_is_denied(self):
        """Each hand-printed form from the spec is denied: conditionals, printf, single quotes.

        A worker usually wraps the sentinel in a check; missing any wrapper reopens the fake-WIN hole.
        """
        # GIVEN the must-deny commands
        cmds = ['[ "$fails" -eq 0 ] && echo "<promise>WIN</promise>"',
                'test -z "$e" && echo "<promise>WIN</promise>"',
                'if [ $f = 0 ]; then echo "<promise>WIN</promise>"; fi',
                'printf "<promise>WIN</promise>\\n"',
                "printf '%s\\n' '<promise>WIN</promise>'"]
        # WHEN each is tested
        got = {c: agent_cli(self.p, "hook", "test", "--cmd", c, "--json") for c in cmds}
        # THEN each one is denied by the verdict rule
        for c, p in got.items():
            self.assertEqual(p.returncode, 2, c)
            self.assertEqual(json.loads(p.stdout)["decision"], "deny", c)
            self.assertEqual(json.loads(p.stdout)["rules"][0]["id"], VERDICT, c)

    def test_deny_names_absolute_path_when_agent_cli_is_not_on_path(self):
        """With PATH wiring broken, the deny message names .claude/bin/agent-cli by absolute path.

        Otherwise the worker is told to run a command its shell cannot find.
        """
        # GIVEN a PATH without agent-cli
        env = {"PATH": "/usr/bin:/bin"}
        # WHEN the hook denies
        p = agent_cli(self.p, "hook", "pretooluse", stdin=bash_event('echo "<promise>WIN</promise>"'), env=env)
        # THEN the message carries the absolute path, not the bare name
        reason = self.decision(p)["permissionDecisionReason"]
        self.assertIn(f'"$({self.p}/.claude/bin/agent-cli eval lib --sh)"', reason)

    def test_bare_name_when_agent_cli_is_on_path(self):
        """With agent-cli on PATH the message keeps the short command."""
        # GIVEN PATH with .claude/bin
        # WHEN the hook denies
        reason = self.decision(self.hook('echo "<promise>WIN</promise>"'))["permissionDecisionReason"]
        # THEN the bare name is used
        self.assertIn('"$(agent-cli eval lib --sh)"', reason)

    def test_hand_written_pr_poll_loops_are_denied(self):
        """A hand-written `while/until ... gh pr checks ... sleep` loop is denied, pointing at `agent-cli pr wait`.

        A foreground poll loop dies with the turn and hand-rolls the green definition each time.
        """
        # GIVEN the loop shapes agents write: both orders, the poll in the condition, the sleep as the
        # condition, and --watch
        cmds = ["while true; do gh pr checks 12; sleep 60; done",
                "until gh pr checks 12 | grep -q pass; do sleep 30; done",
                'until [ "$(gh pr view 12 --json state -q .state)" = MERGED ]; do sleep 60; done',
                'while [ "$(gh pr checks 12 | grep -c pending)" != 0 ]; do sleep 30; done',
                "while :; do\n  sleep 30\n  gh api repos/o/r/pulls/12 | jq .mergeable_state\ndone",
                "while sleep 30; do gh pr checks 12 && break; done",
                "until sleep 30 && gh pr checks 12; do :; done",
                "gh pr checks 12 --watch --interval 60"]
        for c in cmds:
            # WHEN checked
            r = agent_cli(self.p, "hook", "test", "--cmd", c, "--json")
            got = json.loads(r.stdout)
            # THEN denied by hand-pr-poll alone (never also the sleep-poll warn), naming the replacement
            with self.subTest(cmd=c):
                self.assertEqual(r.returncode, 2)
                self.assertEqual([x["id"] for x in got["rules"]], ["hand-pr-poll"])
                self.assertIn("agent-cli pr wait <n> --cap 40m", got["rules"][0]["message"])

    def test_pr_poll_loop_in_quoted_text_is_allowed(self):
        """A commit message that quotes a poll loop is text, not a loop: allowed."""
        # GIVEN the loop inside a quoted commit message
        c = 'git commit -m "replace while true; do gh pr checks 12; sleep 60; done with agent-cli pr wait"'
        # WHEN checked
        r = agent_cli(self.p, "hook", "test", "--cmd", c, "--json")
        # THEN allowed
        self.assertEqual(r.returncode, 0, r.stdout)


class Warn(HookCase):
    def test_sleep_poll_loop_warns_without_a_decision(self):
        """A foreground while/until loop sleeping 3s+ gets the wait hint as context and no permissionDecision.

        Warn, not deny: a generic loop is too broad to block safely, so the command must still run.
        """
        # GIVEN a hand-written poll loop
        # WHEN the hook reads it
        out = self.decision(self.hook("until test -f /tmp/done; do sleep 30; done"))
        # THEN it carries the wait hint and no decision
        self.assertNotIn("permissionDecision", out)
        self.assertIn("agent-cli wait --until", out["additionalContext"])
        self.assertIn("allow hand-sleep-poll", out["additionalContext"])


class SafetyRules(HookCase):
    def test_garbage_stdin_prints_nothing_and_exits_0(self):
        """Fail open: a hook that crashes or errors must never block a session's Bash call."""
        # GIVEN non-JSON stdin
        # WHEN the hook runs
        p = agent_cli(self.p, "hook", "pretooluse", stdin="garbage")
        # THEN nothing is printed and it exits 0
        self.assertEqual((p.returncode, p.stdout), (0, ""))

    def test_kill_switch_disables_every_rule(self):
        """AGENT_CLI_HOOKS=off lets a person turn all rules off when one misfires."""
        # GIVEN the kill switch
        # WHEN a denied command is sent
        p = self.hook('echo "<promise>WIN</promise>"', env={"AGENT_CLI_HOOKS": "off"})
        # THEN nothing is decided
        self.assertIsNone(self.decision(p))

    def test_bypass_comment_skips_only_that_rule(self):
        """`# agent-cli: allow <id>` lets a deliberate command through; other ids do not."""
        # GIVEN the bypass comment for the verdict rule, and one naming another rule
        # WHEN the hook reads each command
        ok = self.hook('echo "<promise>WIN</promise>"  # agent-cli: allow agent-cli-hand-verdict')
        other = self.hook('echo "<promise>WIN</promise>"  # agent-cli: allow some-other-rule')
        # THEN only the matching bypass lets it through
        self.assertIsNone(self.decision(ok))
        self.assertEqual(self.decision(other)["permissionDecision"], "deny")

    def test_rule_whose_required_command_is_missing_never_fires(self):
        """A rule pointing at a command the machine lacks would deny with no way forward."""
        # GIVEN two project rules on the same command, one requiring a missing tool
        rules = [{"id": "needs-missing", "match": "{CMD}frobnicate\\b", "mode": "deny", "message": "m",
                  "requires": "no-such-tool-xyz"},
                 {"id": "needs-sh", "match": "{CMD}frobnicate\\b", "mode": "deny", "message": "m", "requires": "sh"}]
        p = make_project(self.tmp / "req", {"pretooluse-rules.json": json.dumps(rules)})
        # WHEN the command is tested
        r = agent_cli(p, "hook", "test", "--cmd", "frobnicate now", "--json")
        # THEN only the rule with a present tool fires
        self.assertEqual([x["id"] for x in json.loads(r.stdout)["rules"]], ["needs-sh"])

    def test_non_bash_tools_are_never_inspected(self):
        """Only Bash commands are checked; Write/Edit/Read payloads pass whatever they contain."""
        # GIVEN a Write payload carrying the sentinel command
        # WHEN the hook reads it
        p = self.hook('echo "<promise>WIN</promise>"', tool="Write")
        # THEN nothing is decided
        self.assertIsNone(self.decision(p))

    def test_bad_project_rules_file_is_ignored_with_one_stderr_line(self):
        """A broken project rules file names itself on stderr; built-in rules keep working."""
        # GIVEN a project with an invalid rules JSON
        p = make_project(self.tmp / "bad", {"pretooluse-rules.json": "[{not json"})
        # WHEN the hook denies a built-in case
        r = agent_cli(p, "hook", "pretooluse", stdin=bash_event('echo "<promise>WIN</promise>"'))
        # THEN the built-in still denies and stderr names the file once
        self.assertEqual(json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(len(r.stderr.strip().splitlines()), 1, r.stderr)
        self.assertIn("pretooluse-rules.json", r.stderr)

    def project_rules(self, name, rules):
        """hook test --json on the sentinel command and on `frob x`, in a project with these seam rules."""
        p = make_project(self.tmp / name, {"pretooluse-rules.json": json.dumps(rules)})
        res = [agent_cli(p, "hook", "test", "--cmd", c, "--json") for c in ('echo "<promise>WIN</promise>"', "frob x")]
        return [[x["id"] for x in json.loads(r.stdout)["rules"]] for r in res], res[0].stderr

    GOOD = {"id": "good", "match": "{CMD}frob\\b", "mode": "warn", "message": "m"}

    def assert_only_bad_rule_skipped(self, bad, label):
        """The bad rule is skipped with one stderr line naming the file and label; built-ins and `good` stay."""
        # WHEN both commands are tested with the bad rule ahead of a good one
        fired, err = self.project_rules(label.strip("#"), [bad, self.GOOD])
        # THEN the built-in deny and the good rule still fire, and stderr names the file and the rule once
        self.assertEqual(fired, [[VERDICT], ["good"]], err)
        self.assertEqual(len(err.strip().splitlines()), 1, err)
        self.assertIn("pretooluse-rules.json", err)
        self.assertIn(label, err)

    def test_bad_project_regex_skips_only_that_rule(self):
        """A rule whose regex does not compile is dropped alone, never the file or the built-ins."""
        # GIVEN a rule with an unbalanced regex
        self.assert_only_bad_rule_skipped({"id": "bad-re", "match": "(", "mode": "deny", "message": "m"}, "bad-re")

    def test_rule_missing_match_skips_only_that_rule(self):
        """A rule with no `match` would KeyError at evaluate time; it is skipped at load instead."""
        # GIVEN a rule without match
        self.assert_only_bad_rule_skipped({"id": "no-match", "mode": "deny", "message": "m"}, "no-match")

    def test_non_string_match_skips_only_that_rule(self):
        # GIVEN a rule whose match is a list
        self.assert_only_bad_rule_skipped({"id": "list-match", "match": ["x"], "mode": "deny", "message": "m"},
                                          "list-match")

    def test_non_string_requires_skips_only_that_rule(self):
        # GIVEN a rule whose requires is a number
        self.assert_only_bad_rule_skipped({"id": "num-req", "match": "frob", "requires": 3, "mode": "deny",
                                           "message": "m"}, "num-req")

    def test_non_string_needs_skips_only_that_rule(self):
        # GIVEN a rule whose needs is an object
        self.assert_only_bad_rule_skipped({"id": "obj-needs", "match": "frob", "needs": {}, "mode": "deny",
                                           "message": "m"}, "obj-needs")

    def test_non_string_unless_skips_only_that_rule(self):
        # GIVEN a rule whose unless is null
        self.assert_only_bad_rule_skipped({"id": "null-unless", "match": "frob", "unless": None, "mode": "deny",
                                           "message": "m"}, "null-unless")

    def test_bad_mode_skips_only_that_rule(self):
        # GIVEN a rule with an unknown mode
        self.assert_only_bad_rule_skipped({"id": "bad-mode", "match": "frob", "mode": "block", "message": "m"},
                                          "bad-mode")

    def test_rule_without_id_is_named_by_index(self):
        """A rule with no usable id is named by its position in the array."""
        # GIVEN a non-object entry at index 0
        self.assert_only_bad_rule_skipped("not a rule", "#0")

    def test_project_rule_reusing_a_builtin_id_is_rejected(self):
        """A project rule cannot shadow a built-in: its id would also make the built-in's bypass ambiguous."""
        # GIVEN a project warn rule reusing the built-in deny's id
        self.assert_only_bad_rule_skipped({"id": VERDICT, "match": "frob", "mode": "warn", "message": "m"}, VERDICT)

    def test_builtin_deny_fires_with_a_bad_project_rule_present(self):
        """The hand-verdict deny is never switched off by a broken project rule (the pretooluse path)."""
        # GIVEN a project whose only rule is broken
        p = make_project(self.tmp / "deny", {"pretooluse-rules.json": json.dumps(
            [{"id": "broken", "mode": "deny", "message": "m"}])})
        # WHEN the hook reads a hand-printed sentinel
        r = agent_cli(p, "hook", "pretooluse", stdin=bash_event('echo "<promise>WIN</promise>"'))
        # THEN it is denied by the built-in
        out = json.loads(r.stdout)["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertIn(VERDICT, out["permissionDecisionReason"])

    def test_commands_over_20kb_are_not_matched(self):
        """Python re cannot be interrupted, so huge commands skip matching to bound hook time."""
        # GIVEN a denied command padded past 20 KB
        cmd = 'echo "<promise>WIN</promise>"; : ' + "x" * 20 * 1024
        # WHEN the hook reads it
        # THEN nothing is decided
        self.assertIsNone(self.decision(self.hook(cmd)))

    def test_command_just_under_20kb_is_still_matched(self):
        """The bound is exclusive of the limit: a command of exactly 20 KB is still checked."""
        # GIVEN a denied command padded to exactly 20 KB
        head = 'echo "<promise>WIN</promise>"; : '
        cmd = head + "x" * (hook.MAX_CMD - len(head))
        self.assertEqual(len(cmd), hook.MAX_CMD)
        # WHEN the hook reads it
        # THEN it is denied
        self.assertEqual(self.decision(self.hook(cmd))["permissionDecision"], "deny")


class RuleFields(HookCase):
    def test_unless_suppresses_a_match(self):
        """`unless` carves out a legitimate form of a matched command (e.g. a dry run)."""
        # GIVEN a rule on `frob` unless --dry-run
        p = make_project(self.tmp / "u", {"pretooluse-rules.json": json.dumps(
            [{"id": "u", "match": "{CMD}frob\\b", "unless": "--dry-run", "mode": "deny", "message": "m"}])})
        # WHEN both forms are tested
        codes = [agent_cli(p, "hook", "test", "--cmd", c).returncode for c in ("frob x", "frob --dry-run x")]
        # THEN only the plain one is denied
        self.assertEqual(codes, [2, 0])

    def test_raw_rules_see_quoted_text(self):
        """`on: raw` rules see literals the code view strips; default rules do not."""
        # GIVEN the same pattern as a raw and a code rule
        rules = [{"id": "raw", "match": "SECRET_TOKEN", "on": "raw", "mode": "warn", "message": "m"},
                 {"id": "code", "match": "SECRET_TOKEN", "mode": "warn", "message": "m"}]
        p = make_project(self.tmp / "raw", {"pretooluse-rules.json": json.dumps(rules)})
        # WHEN a command holds it only inside quotes
        r = agent_cli(p, "hook", "test", "--cmd", 'echo "SECRET_TOKEN"', "--json")
        # THEN only the raw rule fires
        self.assertEqual([x["id"] for x in json.loads(r.stdout)["rules"]], ["raw"])


class Prefixes(HookCase):
    def test_seam_prefix_word_keeps_command_position(self):
        """A project whose agents put a wrapper word before every command lists it; rules still see the tool."""
        # GIVEN a project with prefix `wrap` and a worktree rule
        p = make_project(self.tmp / "pre", {"command-prefixes": "wrap\n", "pretooluse-rules.json": WT_RULE})
        # WHEN the wrapped and env-prefixed commands are tested
        wrapped = agent_cli(p, "hook", "test", "--cmd", "wrap git worktree add -b x ../x")
        env_pre = agent_cli(p, "hook", "test", "--cmd", "FOO=1 env git worktree add -b x ../x")
        plain_list = agent_cli(p, "hook", "test", "--cmd", "wrap git worktree list")
        # THEN both adds are denied, the list is not
        self.assertEqual(wrapped.returncode, 2, wrapped.stdout)
        self.assertEqual(env_pre.returncode, 2, env_pre.stdout)
        self.assertEqual(plain_list.returncode, 0, plain_list.stdout)

    def test_two_word_prefix_is_matched(self):
        """A two-word prefix line works because its inner space becomes \\s+."""
        # GIVEN prefix `run in`
        p = make_project(self.tmp / "two", {"command-prefixes": "run in\n", "pretooluse-rules.json": WT_RULE})
        # WHEN tested
        r = agent_cli(p, "hook", "test", "--cmd", "run  in git worktree add ../x")
        # THEN denied
        self.assertEqual(r.returncode, 2, r.stdout)

    def test_without_the_seam_a_prefix_word_hides_the_tool(self):
        """Control: the same wrapped command passes when no prefix is listed, so the seam is what works."""
        # GIVEN the worktree rule and no command-prefixes file
        p = make_project(self.tmp / "nopre", {"pretooluse-rules.json": WT_RULE})
        # WHEN tested
        r = agent_cli(p, "hook", "test", "--cmd", "wrap git worktree add -b x ../x")
        # THEN allowed
        self.assertEqual(r.returncode, 0, r.stdout)


class CodeView(unittest.TestCase):
    def test_quoted_text_and_comments_are_stripped(self):
        """Commit, PR and bd text never matches a rule: quoted strings become \"\" and comments go."""
        # GIVEN commands whose only rule-like text is quoted or commented
        # WHEN converted to the code view
        out = [hook.code_view(c) for c in ['git commit -m "git worktree add"', "grep -rn 'git worktree add' .",
                                           "ls # git worktree add"]]
        # THEN that text is gone
        self.assertEqual(out, ['git commit -m ""', 'grep -rn "" .', "ls "])

    def test_heredoc_body_inside_command_substitution_is_removed(self):
        """A commit message written via $(cat <<'EOF' ...) loses its body, even with apostrophes and parens."""
        # GIVEN a heredoc commit message holding a sentinel line, an apostrophe and a paren
        cmd = 'git commit -m "$(cat <<\'EOF\'\nfix: don\'t (really)\necho "<promise>WIN</promise>"\nEOF\n)"'
        # WHEN converted
        out = hook.code_view(cmd)
        # THEN the body is gone and the substitution survives
        self.assertNotIn("promise", out)
        self.assertNotIn("really", out)
        self.assertIn("$(cat <<EOF", out)

    def test_command_substitution_inside_double_quotes_is_kept(self):
        """Poll loops hide their gh call in "$(...)"; the code view keeps it at command position."""
        # GIVEN the two canonical poll loops
        loops = ['until [ "$(gh pr view 12 --json state -q .state)" = MERGED ]; do sleep 60; done',
                 'while [ "$(gh pr checks 12 | grep -c pending)" != 0 ]; do sleep 30; done']
        # WHEN converted and searched with CMD-anchored patterns
        views = [hook.code_view(c) for c in loops]
        # THEN gh and sleep sit at command position in both
        for v in views:
            self.assertTrue(re.search(hook_rules.CMD + r"gh\s+pr\b", v), v)
            self.assertTrue(re.search(hook_rules.CMD + r"sleep\s+\d", v), v)
            self.assertIn("-q .state" if "view" in v else "grep -c pending", v)

    def test_exact_sentinel_string_stays_as_a_token(self):
        """A quoted string that is exactly the sentinel (optionally + literal \\n) stays visible."""
        # GIVEN sentinel strings, one with extra text
        # WHEN converted
        out = [hook.code_view(c) for c in ["printf '<promise>WIN</promise>\\n'", 'echo "WIN: <promise>WIN</promise>"']]
        # THEN only the exact one survives
        self.assertEqual(out, ["printf <promise>WIN</promise>", 'echo ""'])

    def test_dollar_hash_is_not_a_comment(self):
        """`$#` and `${#x}` are parameters, not comments; stripping them would hide the rest of the line."""
        # GIVEN a command using $# before a sentinel echo
        # WHEN converted
        out = hook.code_view('[ $# -eq 0 ] && echo "<promise>WIN</promise>"')
        # THEN the echo survives
        self.assertIn("echo <promise>WIN</promise>", out)

    def test_body_never_crosses_done(self):
        """BODY spans a loop body but stops at `done`, so a rule never reaches into the next loop."""
        # GIVEN a pattern `while ... BODY sleep`
        pat = hook_rules.CMD + r"while\b" + hook_rules.BODY + hook_rules.CMD + r"sleep\s+\d"
        # WHEN searched across two loops, the sleep only in the second
        hit = re.search(pat, "while x; do ls; done; for i in 1; do sleep 5; done")
        inner = re.search(pat, "while x; do sleep 5; done")
        # THEN only the in-loop sleep matches
        self.assertIsNone(hit)
        self.assertIsNotNone(inner)


class RulesListing(HookCase):
    def test_rules_lists_builtin_and_project_rules_with_active_state(self):
        """`hook rules --json` shows every rule, its source and whether it can fire here."""
        # GIVEN a project rule requiring a missing tool
        p = make_project(self.tmp / "ls", {"pretooluse-rules.json": json.dumps(
            [{"id": "proj-x", "match": "x", "mode": "warn", "message": "m", "requires": "no-such-tool-xyz"}])})
        # WHEN listed
        r = agent_cli(p, "hook", "rules", "--json")
        rows = {d["id"]: d for d in map(json.loads, r.stdout.splitlines())}
        # THEN both sources appear with the right active flags
        self.assertEqual(rows[VERDICT]["source"], "builtin")
        self.assertTrue(rows[VERDICT]["active"])
        self.assertEqual(rows["proj-x"]["source"], "project")
        self.assertFalse(rows["proj-x"]["active"])


class AllowCorpus(unittest.TestCase):
    def test_no_corpus_command_is_denied(self):
        """Every everyday command in the corpus passes `agent-cli hook test`; expect-warn lines warn exactly once.

        A rule that denies legitimate work blocks every session in every project using the harness.
        """
        # GIVEN the corpus and an installed project
        tmp = Path(os.path.realpath(tempfile.mkdtemp(prefix="agent-cli-corpus-")))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        p = make_project(tmp / "proj")
        entries = corpus_entries(CORPUS)
        # WHEN each runs through hook test
        allowed = 0
        for cmd, warn in entries:
            r = agent_cli(p, "hook", "test", "--cmd", cmd, "--json")
            got = json.loads(r.stdout)
            with self.subTest(cmd=cmd):
                self.assertEqual(r.returncode, 0, cmd)
                self.assertEqual([x["id"] for x in got["rules"]], [warn] if warn else [])
                allowed += r.returncode == 0
        # THEN all are allowed, and there are at least 30
        sys.stderr.write(f"\nallow corpus: {allowed}/{len(entries)} allowed\n")
        self.assertEqual(allowed, len(entries))
        self.assertGreaterEqual(len(entries), 30)


REPEAT_SEAMS = {"command-prefixes": "wrapx\nwrapx raw\n"}  # a made-up wrapper word, never a real one
CHORE = "deploy-tool push --env staging"  # typed as PreToolUse sees it; the recorder saw it wrapped
CHORE_SHAPE = "deploy-tool <arg> --env <v>"


class RepeatCase(HookCase):
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

    def ask(self, command, session="live1", agent=None, env=None, project=None, args=()):
        """The pretooluse hook's decision for command typed in session (and agent)."""
        event = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": command}}
        if session is not None:
            event["session_id"] = session
        if agent:
            event["agent_id"] = agent
        p = agent_cli(project or self.p, "hook", "pretooluse", *args, stdin=json.dumps(event),
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

    def test_a_warn_rule_and_the_repeat_warning_show_together(self):
        """A warn rule's message and the repeat warning share one additionalContext, rule first."""
        # GIVEN a project warn rule on the chore, and the chore cached by hand
        rule = [{"id": "deploy-hint", "match": "{CMD}deploy-tool\\b", "mode": "warn", "message": "use ship"}]
        p = make_project(self.tmp / "warn", dict(REPEAT_SEAMS, **{"pretooluse-rules.json": json.dumps(rule)}))
        self.write_cache({CHORE_SHAPE: {"sessions": 4, "count": 6}})
        # WHEN the chore is typed
        out = self.ask(CHORE, project=p)
        # THEN both messages are there, one per line
        lines = out["additionalContext"].splitlines()
        self.assertEqual(len(lines), 2, lines)
        self.assertIn("use ship", lines[0])
        self.assertIn("hand-written 6 times across 4 sessions", lines[1])

    def test_a_bad_cache_is_silent_and_rule_warnings_still_show(self):
        """A missing, unparseable or wrong-typed cache gives no repeat warning and never drops a rule's warning.

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

    def test_repeats_only_skips_rule_messages_but_still_yields_to_a_deny(self):
        """--repeats-only (a second install beside a project's own hook) prints only the repeat warning,
        and nothing when a rule denies, so the two hooks never show a rule twice."""
        rule = [{"id": "deploy-hint", "match": "{CMD}deploy-tool\\b", "mode": "warn", "message": "use ship"}]
        p = make_project(self.tmp / "ro", dict(REPEAT_SEAMS, **{"pretooluse-rules.json": json.dumps(rule)}))
        self.write_cache({CHORE_SHAPE: {"sessions": 4, "count": 6},
                          "git worktree <arg> -b <v> <path>": {"sessions": 5, "count": 9}})
        # WHEN the warned chore and the denied command go through with --repeats-only
        warned = self.ask(CHORE, project=p, args=("--repeats-only",))
        denied = self.ask("git worktree add -b x ../x", project=p, args=("--repeats-only",))
        # THEN the first carries only the repeat line, the second prints nothing
        self.assertEqual(len(warned["additionalContext"].splitlines()), 1)
        self.assertIn("hand-written 6 times", warned["additionalContext"])
        self.assertIsNone(denied)

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
        # GIVEN every one of those shapes cached
        self.write_cache({s: {"sessions": 9, "count": 40} for s in {**quiet, **loud}})
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

    def test_quiet_reads_whole_segments(self):
        """quiet() is true only when every segment is a generic read; one real tool, or a transform such as
        sort | uniq -c or jq (a recipe a command can wrap), makes it loud."""
        # GIVEN shapes, WHEN each is checked, THEN only all-read ones are quiet
        for s, want in (("cat <path>", True), ("read <path>", True), ("ls -la <path> && wc -l <path>", True),
                        ("node -e <str>", True), ("python3 <heredoc>", True), ("git log --oneline -<n>", True),
                        ("git push origin <arg>", False), ("python3 <path>/loop.py", False),
                        ("grep <str> <path> | psql <arg>", False), ("for f in <arg>; do cat <path>; done", False),
                        ("<path>/deploy.sh", False), ("git log --format=<v> | sort | uniq -c | sort -rn", False),
                        ("cat <path> | jq <str>", False)):
            with self.subTest(shape=s):
                self.assertIs(hook.quiet(s), want)


class UserPromptSubmit(HookCase):
    def prompt(self, text, env=None, project=None):
        p = agent_cli(project or self.p, "hook", "userpromptsubmit",
                      stdin=json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": "s", "prompt": text}),
                      env={"PATH": self.bin + os.pathsep + os.environ["PATH"], **(env or {})})
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)["hookSpecificOutput"] if p.stdout.strip() else None

    def test_each_default_phrase_adds_the_file_a_bead_context(self):
        """Each default phrase, any case, tells the agent to file a CLI bead citing the skill and house rules."""
        for text in ("This should be a CLI.", "honestly this SHOULD BE A COMMAND", "can you make this a command",
                     "Make it into a CLI please", "make that a cli", "turn this into a command",
                     "Turn that into a CLI", "we keep typing this", "We keep doing this by hand"):
            with self.subTest(prompt=text):
                # GIVEN the prompt, WHEN the hook reads it
                out = self.prompt(text)
                # THEN it adds the context, naming the skill, the house rules and the repeats report
                self.assertEqual(out["hookEventName"], "UserPromptSubmit")
                ctx = out["additionalContext"]
                self.assertIn("The user wants this to become a command. File a bead for it", ctx)
                self.assertIn(".claude/skills/cli-for-agents/SKILL.md and house-rules.md", ctx)
                self.assertIn("agent-cli repeats --top 10", ctx)

    def test_ordinary_prompts_add_nothing(self):
        """Prompts that only share words with the phrases add nothing (no context on every turn)."""
        for text in ("should we cache this?", "make this faster", "this should be a client-side check",
                     "turn this into a list", "the command should be shorter", "we keep this file", ""):
            with self.subTest(prompt=text):
                # GIVEN the prompt, WHEN the hook reads it, THEN nothing is printed
                self.assertIsNone(self.prompt(text))

    def test_project_phrase_line_adds_a_regex(self):
        """A `cli-phrases:` regex under `## repeats` in project-rules.md also fires; a bad one is ignored and
        the defaults keep working."""
        good = make_project(self.tmp / "good")
        (good / ".claude/project-rules.md").write_text("## repeats\ncli-phrases: `wrap (it|this) up as a tool`\n")
        bad = make_project(self.tmp / "badre")
        (bad / ".claude/project-rules.md").write_text("## repeats\ncli-phrases: (unclosed\n")
        # GIVEN each project, WHEN prompts are read
        # THEN the project's phrase fires only where it is listed, and the bad regex leaves the defaults
        self.assertIsNotNone(self.prompt("please wrap this up as a tool", project=good))
        self.assertIsNone(self.prompt("please wrap this up as a tool"))
        self.assertIsNotNone(self.prompt("this should be a CLI", project=bad))
        self.assertIsNone(self.prompt("please wrap this up as a tool", project=bad))

    def test_garbage_stdin_and_kill_switch_print_nothing(self):
        """Fail open: bad stdin prints nothing and exits 0; AGENT_CLI_HOOKS=off silences it."""
        # GIVEN garbage stdin, and a matching prompt under the kill switch
        r = agent_cli(self.p, "hook", "userpromptsubmit", stdin="garbage")
        # WHEN each runs, THEN nothing is printed and both exit 0
        self.assertEqual((r.returncode, r.stdout), (0, ""))
        self.assertIsNone(self.prompt("this should be a CLI", env={"AGENT_CLI_HOOKS": "off"}))


class Settings(unittest.TestCase):
    def test_settings_write_the_cache_at_session_start_and_read_prompts(self):
        """settings.json refreshes the repeats cache on SessionStart and runs the phrase hook on
        UserPromptSubmit, each guarded, timeout 5, ending in exit 0 (a project without agent-cli sees no error)."""
        # GIVEN the harness settings
        hooks = json.loads((CLAUDE / "settings.json").read_text())["hooks"]
        for ev, needle in (("SessionStart", " repeats --write-cache --since 14d >/dev/null 2>&1"),
                           ("UserPromptSubmit", " hook userpromptsubmit 2>/dev/null")):
            with self.subTest(event=ev):
                # WHEN the event's entries are read
                entries = [h for g in hooks[ev] for h in g["hooks"] if needle in h["command"]]
                # THEN exactly one, timeout 5, guarded and ending in exit 0
                self.assertEqual(len(entries), 1)
                (h,) = entries
                self.assertEqual(h.get("timeout"), 5)
                self.assertTrue(h["command"].startswith('[ -x "$CLAUDE_PROJECT_DIR/.claude/bin/agent-cli" ]'))
                self.assertTrue(h["command"].endswith("; exit 0"))


if __name__ == "__main__":
    unittest.main()
