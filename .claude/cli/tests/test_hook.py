"""Tests for the PreToolUse hook framework: `agent-cli hook pretooluse|test|rules`, run as a real subprocess."""
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import CLAUDE, CORPUS, agent_cli, bash_event, corpus_entries, make_project

sys.path.insert(0, str(CLAUDE / "cli"))
from agent_cli import hook, hook_rules  # noqa: E402

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


class Warn(HookCase):
    def test_warn_rule_adds_context_without_a_decision(self):
        """A warn rule returns additionalContext and no permissionDecision, so the command still runs."""
        # GIVEN the demo warn command
        # WHEN the hook reads it
        out = self.decision(self.hook("true agent-cli-warn-demo"))
        # THEN it carries context and no decision
        self.assertNotIn("permissionDecision", out)
        self.assertIn("agent-cli-demo-warn", out["additionalContext"])


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

    def test_bad_project_regex_is_ignored(self):
        """A rule whose regex does not compile drops the project file, not the hook."""
        # GIVEN a rules file with an unbalanced regex
        p = make_project(self.tmp / "re", {"pretooluse-rules.json": json.dumps(
            [{"id": "x", "match": "(", "mode": "deny", "message": "m"}])})
        # WHEN tested
        r = agent_cli(p, "hook", "test", "--cmd", "ls")
        # THEN it allows and names the file
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("pretooluse-rules.json", r.stderr)

    def test_commands_over_20kb_are_not_matched(self):
        """Python re cannot be interrupted, so huge commands skip matching to bound hook time."""
        # GIVEN a denied command padded past 20 KB
        cmd = 'echo "<promise>WIN</promise>"; : ' + "x" * 20 * 1024
        # WHEN the hook reads it
        # THEN nothing is decided
        self.assertIsNone(self.decision(self.hook(cmd)))


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


if __name__ == "__main__":
    unittest.main()
