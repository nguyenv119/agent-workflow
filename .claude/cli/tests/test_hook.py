"""Tests for the PreToolUse hook framework: `agent-cli hook pretooluse|test|rules`, run as a real subprocess."""
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import helpers
from helpers import CLAUDE, CORPUS, agent_cli, bash_event, corpus_entries, make_project

sys.path.insert(0, str(CLAUDE / "cli"))
from agent_cli import hook, hook_rules  # noqa: E402

VERDICT = "agent-cli-hand-verdict"
WT_RULE = json.dumps([{"id": "test-wt", "match": r"{CMD}git\s+worktree\s+add\b", "mode": "deny",
                       "message": "use the worktree command"}])


class HookCase(helpers.HookCase):
    def hook(self, command, tool="Bash", env=None, project=None):
        return super().hook(bash_event(command, tool), env, project)


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

    def fired(self, cmd):
        """(exit code, ids of the rules that fire) from `agent-cli hook test --cmd cmd --json`."""
        r = agent_cli(self.p, "hook", "test", "--cmd", cmd, "--json")
        return r.returncode, [x["id"] for x in json.loads(r.stdout)["rules"]]

    def assert_fired(self, cases):
        """Each (command, (exit code, rule ids)) pair holds."""
        for cmd, want in cases:
            with self.subTest(cmd=cmd):
                self.assertEqual(self.fired(cmd), want)

    def test_one_shot_pr_view_json_warns_toward_pr_status(self):
        """A one-shot `gh pr view <n> --json ...` runs, with the `agent-cli pr status` hint as context.

        Warn, not deny: a single read is cheap and sometimes the right call; the hint names the command
        that already gives the verdict in one step.
        """
        # GIVEN the one-shot PR state read agents type
        # WHEN the hook reads it
        out = self.decision(self.hook("gh pr view 3612 --json state,mergeStateStatus"))
        # THEN no decision, and the context names agent-cli pr status and the bypass
        self.assertNotIn("permissionDecision", out)
        self.assertIn("agent-cli pr status <n>", out["additionalContext"])
        self.assertIn("allow hand-pr-status", out["additionalContext"])

    def test_each_one_shot_form_warns_its_pointer_rule(self):
        """One-shot PR reads, worktree list/remove and a bare sleep of 20s or more each warn their rule.

        These chores have agent-cli commands (pr status, wt list/rm, wait) that most calls still skip.
        """
        # GIVEN the one-shot forms, including -C, unit suffixes and a chain holding two of them
        # WHEN each is tested
        # THEN each is allowed (exit 0) with exactly its rule(s), in rule order
        status, wt, sleep = ["hand-pr-status"], ["hand-worktree-list-rm"], ["hand-bare-sleep"]
        self.assert_fired([
            ("gh pr view 3612 --json state,mergeStateStatus", (0, status)),
            ("gh pr checks 12", (0, status)),
            ("gh pr checks 12 --json name,bucket", (0, status)),
            ("git worktree list", (0, wt)),
            ("git -C /x worktree list", (0, wt)),
            ("git worktree remove /x", (0, wt)),
            ("sleep 20", (0, sleep)),
            ("sleep 100", (0, sleep)),
            ("sleep 300s", (0, sleep)),
            ("sleep 2m", (0, sleep)),
            ("sleep 1h", (0, sleep)),
            ("git push && sleep 30 && gh pr view 12 --json url", (0, status + sleep)),
        ])

    def test_pr_poll_loops_stay_with_the_poll_deny_alone(self):
        """A PR poll loop and `gh pr checks --watch` get hand-pr-poll's deny and no pointer warn on top.

        The deny already names the replacement; a second hint for the same command would only add noise.
        """
        # GIVEN a poll loop with the poll in the condition, and --watch
        # WHEN each is tested
        # THEN denied by hand-pr-poll alone
        self.assert_fired([("while ! gh pr checks 12; do sleep 30; done", (2, ["hand-pr-poll"])),
                           ("gh pr checks 12 --watch", (2, ["hand-pr-poll"]))])

    def test_sleeps_and_pr_reads_inside_a_loop_are_not_bare(self):
        """A sleep or PR read inside a for/while/until loop is left to the loop rules (or nothing).

        hand-sleep-poll covers while/until loops and counted `for` loops; a `for` over a fixed list is not
        a wait (unless it polls a commit's CI state), and a PR read per item of a list is not a one-shot
        status check.
        """
        # GIVEN loops holding a long sleep or a PR read, on one line and across lines
        # WHEN each is tested
        # THEN no pointer rule fires: only hand-sleep-poll, on the while/until loops
        self.assert_fired([
            ("for i in 1 2 3; do sleep 30; done", (0, [])),
            ("for n in 53 54; do\n  gh pr view $n --json state\n  sleep 30\ndone", (0, [])),
            ("while true; do sleep 30; done", (0, ["hand-sleep-poll"])),
            ("until test -f /tmp/x; do\n  sleep 60\ndone", (0, ["hand-sleep-poll"])),
        ])

    def test_counted_for_loops_and_commit_ci_polls_warn_sleep_poll(self):
        """A counted `for` loop sleeping 3s+, and any loop polling a commit's CI state, warn hand-sleep-poll.

        `for i in $(seq 1 40)` is a wait with a hand-picked cap; a loop over a commit's status or check-runs
        is a CI wait even over a fixed list. Warn, not the PR-poll deny: a commit may have no PR.
        """
        # GIVEN counted for loops in each shell form, and loops polling a commit's status or check-runs
        # WHEN each is tested
        # THEN each is allowed (exit 0) with hand-sleep-poll alone
        poll = ["hand-sleep-poll"]
        self.assert_fired([
            ("for i in $(seq 1 40); do gh api repos/o/r/commits/abc123/status --jq .state | grep -q success "
             "&& break; sleep 30; done", (0, poll)),
            ("for i in $(seq 30); do test -s out && break; sleep 20; done", (0, poll)),
            ("for i in {1..30}; do test -s out && break; sleep 20; done", (0, poll)),
            ("for ((i=0; i<30; i++)); do test -s out && break; sleep 20; done", (0, poll)),
            ("for i in 1 2 3 4 5; do\n  gh api repos/o/r/commits/$sha/check-runs\n  sleep 30\ndone", (0, poll)),
        ])

    def test_piped_agent_cli_wait_warns(self):
        """`agent-cli wait` or `agent-cli pr wait` piped into tail/head warns piped-wait.

        The pipe's exit is the last command's, so a timeout (4) or a failure (3) reads as 0.
        """
        # GIVEN waits piped with |, 2>&1 | and |&
        # WHEN each is tested
        # THEN each is allowed (exit 0) with piped-wait alone
        piped = ["piped-wait"]
        self.assert_fired([
            ("agent-cli wait --until 'test -s out' --every 30 --cap 10m | tail -1", (0, piped)),
            ("agent-cli wait --until 'test -s out' --cap 10m 2>&1 | tail -5", (0, piped)),
            ("agent-cli pr wait 12 --cap 40m | tail -3", (0, piped)),
            ("agent-cli wait --until x |& head", (0, piped)),
            (".claude/bin/agent-cli wait --until x --cap 5m | tail -1", (0, piped)),
        ])

    def test_near_misses_of_the_wait_rules_are_silent(self):
        """Unpiped waits, a pipe inside --until, ||, redirects, --help, one-shot reads and short or
        quoted loops stay silent.

        A warn that fires on look-alikes teaches agents to ignore every warn.
        """
        # GIVEN look-alikes of piped-wait and of the new hand-sleep-poll shapes
        # WHEN each is tested
        # THEN none fires
        self.assert_fired([
            ("agent-cli wait --until 'test -s out' --every 30 --cap 10m", (0, [])),
            ("agent-cli wait --until 'gh pr checks 12 | grep -q pass' --cap 10m", (0, [])),
            ("agent-cli wait --until x --cap 5m || echo timed out", (0, [])),
            ("agent-cli wait --until x --cap 5m > out.json", (0, [])),
            ("agent-cli wait --help | head", (0, [])),
            ("agent-cli wait --until x 2>&1 | tail -1; echo ${PIPESTATUS[0]}", (0, [])),
            ("gh api repos/o/r/commits/abc123/status --jq .state", (0, [])),
            ("for i in $(seq 1 3); do echo $i; done", (0, [])),
            ("for i in $(seq 1 5); do sleep 1; done", (0, [])),
            ('git commit -m "for i in $(seq 1 3); do sleep 30; done"', (0, [])),
        ])

    def test_near_misses_of_the_pointer_rules_are_silent(self):
        """Short sleeps, PR reads without --json, other worktree verbs and quoted text stay silent.

        A warn that fires on look-alikes teaches agents to ignore every warn.
        """
        # GIVEN look-alikes of each new rule, and worktree adds that keep their old rules
        # WHEN each is tested
        # THEN none fires a pointer rule; the adds still get exactly their old rule
        self.assert_fired([
            ("sleep 5", (0, [])),
            ("sleep 19", (0, [])),
            ("sleep 10s", (0, [])),
            ("gh pr view 12", (0, [])),
            ("gh pr view 12 --web", (0, [])),
            ("gh pr list --json number", (0, [])),
            ("git worktree prune", (0, [])),
            ('git commit -m "sleep 60 then gh pr checks 12 and git worktree list"', (0, [])),
            ("git worktree add --detach ../wt HEAD", (0, [])),
            ("git worktree add ../review-12 origin/some-branch", (0, ["hand-worktree-add-other"])),
            ("git worktree add -b feat ../wt origin/main", (2, ["hand-worktree-add"])),
        ])


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

    def test_bash_rules_never_run_on_other_tools(self):
        """The command rules check Bash only; Write/Read payloads carrying a command pass.

        Edit tools are checked for harness paths alone (test_harness_edit.py); a command-shaped string
        in their input is file content, not something a shell will run.
        """
        for tool in ("Write", "Read"):
            with self.subTest(tool=tool):
                # GIVEN a payload of this tool carrying the sentinel command
                # WHEN the hook reads it
                p = self.hook('echo "<promise>WIN</promise>"', tool=tool)
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

    def test_notification_turns_add_nothing_even_with_a_phrase(self):
        """Turns the harness starts (a background agent's result, a system notification, Stop-hook feedback, a
        peer session, a scheduled task, a compaction resume) are not the user asking: a phrase inside one adds
        nothing, also after leading blank lines or a system-reminder block."""
        report = "Done. Note: this lookup should be a CLI."
        for text in (f"<task-notification>\n<task-id>abc</task-id>\n<status>completed</status>\n"
                     f"<result>{report}</result>\n</task-notification>",
                     f"<task-notification>\n<summary>Stop hook feedback: {report}</summary>\n</task-notification>",
                     f"[SYSTEM NOTIFICATION - NOT USER INPUT]\n{report}",
                     f"Stop hook feedback:\n{report}",
                     f"Another Claude session sent a message: {report}",
                     f"<scheduled-task name=\"daily\">{report}</scheduled-task>",
                     f"<ci-monitor-event>{report}</ci-monitor-event>",
                     f"[Cross-session idle notice] {report}",
                     f"This session is being continued from a previous conversation that ran out of context. {report}",
                     f"\n\n  <task-notification>{report}</task-notification>",
                     f"<system-reminder>\nline one\nline two\n</system-reminder>\n"
                     f"<task-notification>{report}</task-notification>"):
            with self.subTest(prompt=text[:40]):
                # GIVEN a harness-started turn carrying the phrase, WHEN the hook reads it, THEN nothing is printed
                self.assertIsNone(self.prompt(text))

    def test_marker_text_not_at_the_start_still_triggers(self):
        """Only a prompt that opens with a marker is skipped: the user quoting a marker mid-message, or a
        system-reminder ahead of their own words, still gets the context."""
        for text in ("I saw <task-notification> in the log; this should be a CLI",
                     "<system-reminder>\nline one\nline two\n</system-reminder>\nthis should be a CLI",
                     "> Stop hook feedback: x\nthis should be a CLI",
                     "<system-reminder>a</system-reminder>\nthis should be a CLI\n"
                     "<system-reminder>b</system-reminder><task-notification>"):
            with self.subTest(prompt=text[:40]):
                # GIVEN the user's own prompt, WHEN the hook reads it, THEN the context is added
                self.assertIsNotNone(self.prompt(text))

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

    def test_settings_run_pretooluse_on_the_edit_tools(self):
        """settings.json runs `hook pretooluse` for Edit|Write|MultiEdit|NotebookEdit with the Bash group's
        guarded command, so harness edits on main reach the harness-edit-on-main check."""
        # GIVEN the harness settings
        groups = json.loads((CLAUDE / "settings.json").read_text())["hooks"]["PreToolUse"]
        bash = [h for g in groups if g.get("matcher") == "Bash" for h in g["hooks"] if " hook pretooluse" in h["command"]]
        # WHEN the edit-tools group is read
        edit = [h for g in groups if g.get("matcher") == "Edit|Write|MultiEdit|NotebookEdit" for h in g["hooks"]]
        # THEN it has exactly the Bash group's command and timeout
        self.assertEqual(len(edit), 1)
        self.assertEqual((edit[0]["command"], edit[0].get("timeout")), (bash[0]["command"], 5))


if __name__ == "__main__":
    unittest.main()
