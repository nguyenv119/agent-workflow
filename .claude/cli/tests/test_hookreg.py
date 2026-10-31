"""Tests for `agent-cli harness hooks`: every hook in the settings files needs a registry entry (reason, owner,
review_by), and an entry whose review_by is in the past is flagged.

Why: a guard outlives the problem it was written for unless someone has to say again why it is still there. Run
through the real CLI on real settings and registry files in temp dirs.
"""
import datetime
import json
import sys
import unittest

from helpers import CLAUDE, TmpCase, agent_cli, make_project

sys.path.insert(0, str(CLAUDE / "cli"))
from agent_cli import hookreg  # noqa: E402

FUTURE, PAST = "2999-01-01", "2000-01-01"
GUARD = 'f="$HOME/g.py"; [ -f "$f" ] || exit 0; exec python3 "$f"'
NOTE = "echo hello"


def entry(review_by=FUTURE, reason="stops a repeat of the 3 Oct incident", owner="alice"):
    return {"reason": reason, "owner": owner, "review_by": review_by}


def settings(*pairs):
    """A settings.json body with one hook per (event, command)."""
    out = {}
    for event, command in pairs:
        out.setdefault(event, []).append({"matcher": "Bash", "hooks": [{"type": "command", "command": command}]})
    return {"hooks": out}


class Hooks(TmpCase):
    def setUp(self):
        super().setUp()
        self.p = make_project(self.tmp / "proj")
        self.settings = self.tmp / "settings.json"
        self.registry = self.tmp / "reasons.json"

    def go(self, hooks, reg, *extra):
        self.settings.write_text(json.dumps(settings(*hooks)))
        self.registry.write_text(json.dumps(reg))
        return agent_cli(self.p, "harness", "hooks", "--registry", str(self.registry),
                         "--settings", str(self.settings), *extra)

    def test_lists_each_hook_with_reason_and_date_and_exits_0(self):
        """
        Verifies a registry that covers every hook gives exit 0 and a row with the reason and review date.

        This matters because the row is how a reviewer sees why a guard exists and when to look at it again.

        If it breaks, the weekly review reads nothing, or a clean registry fails the check.
        """
        # GIVEN two hooks, both registered
        # WHEN the checker runs
        p = self.go([("Stop", GUARD), ("SessionStart", NOTE)], {GUARD: entry(), NOTE: entry(reason="greets")})
        # THEN exit 0, each row carries date and reason, and the summary counts both
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn(f"ok {FUTURE} alice Stop Bash", p.stdout)
        self.assertIn("greets", p.stdout)
        self.assertIn("hooks: 2 listed, 2 ok, 0 missing, 0 past review_by, 0 invalid", p.stdout)

    def test_hook_without_entry_is_flagged_with_the_fix(self):
        """
        Verifies a hook with no registry entry is reported `missing` with an entry to paste, exit 1.

        This matters because an unexplained guard is exactly what this check exists to stop.

        If it breaks, a new hook ships without a reason and nobody ever reviews it.
        """
        # GIVEN one registered and one unregistered hook
        # WHEN the checker runs
        p = self.go([("Stop", GUARD), ("Stop", NOTE)], {GUARD: entry()})
        # THEN exit 1, the unregistered one is named, and the summary counts it
        self.assertEqual(p.returncode, 1)
        self.assertIn(f"missing - - Stop Bash {NOTE}", p.stdout)
        self.assertIn("fix: add to the registry", p.stdout)
        self.assertIn("1 missing", p.stdout)

    def test_past_review_by_is_flagged_and_exits_1(self):
        """
        Verifies an entry whose review_by is in the past is reported `past` and exits 1.

        This matters because the date is the only thing that makes a guard get looked at again.

        If it breaks, a stale guard stays forever with a reason nobody re-checked.
        """
        # GIVEN a registered hook whose review date passed
        # WHEN the checker runs
        p = self.go([("Stop", GUARD)], {GUARD: entry(review_by=PAST)})
        # THEN it is flagged
        self.assertEqual(p.returncode, 1)
        self.assertIn(f"past {PAST}", p.stdout)
        self.assertIn("1 past review_by", p.stdout)

    def test_review_by_today_is_not_past(self):
        """
        Verifies the day of review_by itself still passes and the day after fails.

        This matters because the date is the last day the reason stands; flagging it a day early makes the
        owner act before they are due.

        If it breaks, a reviewer sees a failure on the very day they planned to review.
        """
        # GIVEN today's date
        today = datetime.date(2030, 5, 5)
        # WHEN the same entry is checked on its own date and the day after
        on = hookreg.check([("s", "Stop", None, NOTE)], {NOTE: entry("2030-05-05")}, today)
        after = hookreg.check([("s", "Stop", None, NOTE)], {NOTE: entry("2030-05-05")}, today + datetime.timedelta(1))
        # THEN
        self.assertEqual([on[0]["status"], after[0]["status"]], ["ok", "past"])

    def test_entry_without_reason_or_with_bad_date_is_invalid(self):
        """
        Verifies an entry with an empty reason, or a review_by that is not YYYY-MM-DD, is `invalid`.

        This matters because a blank reason satisfies "has an entry" while explaining nothing.

        If it breaks, the registry fills with placeholders and the check passes.
        """
        # GIVEN two hooks, one with a blank reason, one with a date that is not ISO
        reg = {GUARD: entry(reason="  "), NOTE: entry(review_by="next week")}
        # WHEN the checker runs
        p = self.go([("Stop", GUARD), ("Stop", NOTE)], reg)
        # THEN both are invalid
        self.assertEqual(p.returncode, 1)
        self.assertIn("2 invalid", p.stdout)

    def test_unused_registry_entry_is_listed_but_does_not_fail(self):
        """
        Verifies a registry key matching no hook is printed as `unused` and exit stays 0.

        This matters because a removed hook leaves its entry behind; the owner should see it, but a leftover
        must not block the check.

        If it breaks, deleting a hook breaks the check, or leftovers pile up unseen.
        """
        # GIVEN a registry with one entry too many
        # WHEN the checker runs
        p = self.go([("Stop", GUARD)], {GUARD: entry(), NOTE: entry()})
        # THEN it is listed, exit 0
        self.assertEqual(p.returncode, 0)
        self.assertIn(f"unused: {NOTE}", p.stdout)

    def test_json_mode_prints_one_object_with_the_rows(self):
        """
        Verifies --json prints one JSON object whose hooks carry status, event, command, reason and review_by.

        This matters because the weekly gardener reads the past-date list by machine.

        If it breaks, the gardener must scrape text rows.
        """
        # GIVEN one past hook
        # WHEN run with --json
        p = self.go([("Stop", GUARD)], {GUARD: entry(review_by=PAST)}, "--json")
        # THEN one line of JSON with the row
        out = json.loads(p.stdout)
        row = out["hooks"][0]
        self.assertEqual((p.returncode, len(p.stdout.splitlines())), (1, 1))
        self.assertEqual((row["status"], row["event"], row["command"], row["review_by"], row["reason"]),
                         ("past", "Stop", GUARD, PAST, "stops a repeat of the 3 Oct incident"))
        self.assertEqual(out["counts"]["past"], 1)

    def test_one_command_under_two_events_is_one_entry_two_rows(self):
        """
        Verifies a command wired to two events is listed twice and covered by one registry key.

        This matters because the registry is keyed by command, and the same script often runs on start and end.

        If it breaks, one of the two rows shows `missing` though its reason is written.
        """
        # GIVEN the same command on two events
        # WHEN the checker runs
        p = self.go([("SessionStart", NOTE), ("SessionEnd", NOTE)], {NOTE: entry()})
        # THEN two ok rows
        self.assertEqual(p.returncode, 0, p.stdout)
        self.assertIn("2 listed, 2 ok", p.stdout)

    def test_bad_inputs_exit_2_with_an_example(self):
        """
        Verifies a missing registry, invalid JSON and a missing --settings file exit 2 naming a correct call.

        This matters because exit 1 means "checked and failed"; a typo must not look like a failed check.

        If it breaks, a caller cannot tell an unchecked run from a failed one.
        """
        # GIVEN a settings file and a registry that is not JSON
        self.settings.write_text(json.dumps(settings(("Stop", NOTE))))
        self.registry.write_text("{nope")
        run = lambda *a: agent_cli(self.p, "harness", "hooks", *a)  # noqa: E731
        # WHEN each bad call runs
        bad_json = run("--registry", str(self.registry), "--settings", str(self.settings))
        no_file = run("--registry", str(self.tmp / "none.json"), "--settings", str(self.settings))
        no_settings = run("--registry", str(self.registry), "--settings", str(self.tmp / "none.json"))
        no_registry = run("--settings", str(self.settings))
        # THEN all exit 2 and show an example
        for p in (bad_json, no_file, no_settings, no_registry):
            self.assertEqual(p.returncode, 2, p.stderr)
            self.assertIn("agent-cli harness hooks --registry", p.stderr)

    def test_blank_owner_and_non_object_registry_are_refused(self):
        """
        Verifies a blank owner marks the entry invalid (exit 1) and a registry that is not a JSON object exits 2.

        This matters because an entry nobody owns cannot be reviewed, and a list registry is a wrong file, not a clean one.

        If it breaks, ownerless guards pass the check or the checker crashes on a wrong file.
        """
        # GIVEN one hook whose entry has a blank owner, then a registry that is a JSON list
        blank = self.go([("Stop", NOTE)], {NOTE: entry(owner=" ")})
        as_list = self.go([("Stop", NOTE)], [])
        # THEN the blank owner is invalid with exit 1, and the list is refused with exit 2
        self.assertEqual(blank.returncode, 1, blank.stderr)
        self.assertIn("1 invalid", blank.stdout)
        self.assertEqual(as_list.returncode, 2, as_list.stderr)
        self.assertNotIn("Traceback", as_list.stderr)

    def test_several_settings_files_are_all_listed(self):
        """
        Verifies every --settings file is read, one row per file and hook.

        This matters because user and project hooks live in different files and both need a reason.

        If it breaks, hooks in the second file go unchecked.
        """
        # GIVEN two settings files with one hook each and a registry covering only the first
        other = self.tmp / "other.json"
        other.write_text(json.dumps(settings(("Stop", NOTE))))
        self.settings.write_text(json.dumps(settings(("Stop", GUARD))))
        self.registry.write_text(json.dumps({GUARD: entry()}))
        # WHEN the checker reads both
        p = agent_cli(self.p, "harness", "hooks", "--registry", str(self.registry),
                      "--settings", str(self.settings), "--settings", str(other))
        # THEN both hooks are listed and the one in the second file is missing
        self.assertEqual(p.returncode, 1, p.stderr)
        self.assertIn("hooks: 2 listed, 1 ok, 1 missing", p.stdout)

    def test_non_string_hook_command_exits_2_not_a_traceback(self):
        """
        Verifies a hook whose command is not a string is refused with exit 2 and an example.

        This matters because a hand-edited settings file can hold any JSON, and a crash looks like a bug in the tool.

        If it breaks, the checker dies with a traceback instead of naming the bad settings file.
        """
        # GIVEN a settings file whose only hook has a list as its command
        self.settings.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": ["a"]}]}]}}))
        self.registry.write_text("{}")
        # WHEN the checker runs
        p = agent_cli(self.p, "harness", "hooks", "--registry", str(self.registry), "--settings", str(self.settings))
        # THEN exit 2 with the example and no traceback
        self.assertEqual(p.returncode, 2, p.stderr)
        self.assertIn("agent-cli harness hooks --registry", p.stderr)
        self.assertNotIn("Traceback", p.stderr)


if __name__ == "__main__":
    unittest.main()
