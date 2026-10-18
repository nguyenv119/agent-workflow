"""Tests for hook.basic() and hook.report_only(): which step shapes are shell basics with no chore in them;
and hook.chore(): the repeat key of a step piped into quiet reads."""
import sys
import unittest

from helpers import CLAUDE

sys.path.insert(0, str(CLAUDE / "cli"))
from agent_cli import hook  # noqa: E402


class BasicSteps(unittest.TestCase):
    def test_basic_is_true_only_when_every_pipeline_segment_is_basic(self):
        """basic(step) is true for shell basics and quiet reads (by name or by path), `cut -c`/`-b`
        truncation, placeholder-only commands, a test whose `[` an old row masked (`<arg> -n <str> <arg>`:
        a placeholder command word, then only placeholders and test operators), git housekeeping (after git's global options) and inline
        interpreter code (after short flags), a closing keyword piped into reads (`done | head <n>`), when every `|` segment is one; one real tool in the
        pipeline makes it false.

        The report drops basic steps and the cache never holds them; a basic that slips through tops the
        list (cat, export), and a real chore marked basic (`echo <str> | psql`) disappears from it.
        """
        # GIVEN steps, WHEN each is checked, THEN only all-basic ones are basic
        for step, want in (("export PATH=<v>", True), ("set -a", True), ("mkdir -p <arg>", True), ("cp <arg>", True),
                           ("git add <arg>", True), ("git commit -m <str>", True), ("git -C <arg> add <arg>", True),
                           ("git --no-pager diff", True), ("cat <arg> | grep <arg>", True), ("cat <arg>", True),
                           ("do mkdir -p <arg>", True), ("FOO=<v> cp <arg>", True), ("python3 -I - <heredoc>", True),
                           ("python3 -c <str>", True), ("node -e <str>", True), ("python3 <heredoc>", True),
                           ("sleep <n>", True), ("git log origin/<arg>", True), ("echo <str> | psql <arg>", False),
                           (". .env", False), ("python3 <path>/loop.py", False), ("python3 -m <arg>", False),
                           ("<path>/cp <arg>", True), ("git worktree add <arg>", False), ("deploy-tool <arg>", False),
                           ("dbtool query <str> | tail <n>", False), ("git -C <arg> worktree list", False),
                           ("[ -n <str> <arg>", True), ("[[ -f <v> <arg>", True), ("seq <n>", True), ("<arg>", True),
                           ("<arg> <str>", True), ("<path>/grep -n <str> <arg> | head <n>", True),
                           ("grep -n <str> <arg> | cut -c<v>", True), ("sed -n <n> <arg> | cut -c <v>", True),
                           ("cat <arg> | cut --characters=<v>", True), ("tail <n> <arg> | cut -b<v>", True),
                           ("cut -d<v> -f<v> <arg>", False), ("grep <arg> .env | cut -d=<v> -f<v>", False),
                           ("cut -c<v> -f<v> <arg>", False), ("<path>/deploy.sh", False),
                           ("<str> --headless=<v> --disable-gpu <url>", False),
                           ("<arg> -n <str> <arg>", True), ("<arg> -f <str> <arg>", True), ("<str> -eq <n> ]", True),
                           ("<v> ! -z <str> -a -d <arg> ]]", True), ("<arg> -n --force <arg>", False),
                           ("<path> -f <arg>", False), ("read <path>", True), ("git log --oneline -<n>", True),
                           ("git log --format=<v> | sort | uniq -c | sort -rn", False), ("cat <path> | jq <str>", False),
                           ("done | head <n>", True), ("fi | tail <n>", True), ("done | psql <arg>", False)):
            with self.subTest(step=step):
                self.assertIs(hook.basic(step), want)

    def test_report_only_is_a_lone_sleep_or_a_git_read_naming_origin(self):
        """report_only(step) is true for a step that is only a sleep, and for git log/show naming
        origin/<arg> (after git's global options); never for a step that is not basic.

        The report must list waits and on-main checks (chores a command can do) while the cache leaves
        them out; marking a real pipeline report-only would keep it off the cache.
        """
        # GIVEN steps, WHEN each is checked, THEN only waits and on-main reads are report-only
        for step, want in (("sleep <n>", True), ("git log origin/<arg>", True), ("git log <arg>..origin/<arg>", True),
                           ("git -C <arg> show origin/<arg>", True), ("git log origin/<arg> | head <n>", True),
                           ("git log --oneline", False), ("git fetch origin <arg>", False), ("cat <arg>", False),
                           ("deploy-tool origin/<arg>", False), ("git log origin/<arg> | psql <arg>", False),
                           ("sleep <n> | head <n>", False)):
            with self.subTest(step=step):
                self.assertIs(hook.report_only(step), want)


class ChoreKeys(unittest.TestCase):
    def test_a_quiet_pipeline_tail_folds_into_the_first_segment(self):
        """chore(step) is the step's first `|` segment when every later segment is basic (head, tail, grep,
        sed -n, cut -c, wc, inline interpreter code), else the whole step; a one-segment step is unchanged.

        The repeat cache and warning are keyed by it, so `bd show <arg> | head <n>` and `bd show <arg> | grep
        <str>` are one chore: as separate keys each drew its own warning and spent the 5-per-session cap on
        variants of one chore. A recipe tail (tr, jq, sort) is part of the chore and must stay.
        """
        # GIVEN steps, WHEN each is keyed, THEN quiet tails fold away and recipe tails stay
        for step, want in (("bd show <arg> | head <n>", "bd show <arg>"), ("bd show <arg> | tail <n>", "bd show <arg>"),
                           ("bd show <arg> | grep -n <str> | head <n>", "bd show <arg>"),
                           ("x-tool <arg> | sed -n <n>", "x-tool <arg>"), ("x-tool <arg> | cut -c<v>", "x-tool <arg>"),
                           ("x-tool <arg> | wc -l", "x-tool <arg>"),
                           ("bd show <arg> --json | python3 -c <str>", "bd show <arg> --json"),
                           ("ls <arg> | tr <str>", "ls <arg> | tr <str>"),
                           ("gh pr view <n> --json <v> | jq <str>", "gh pr view <n> --json <v> | jq <str>"),
                           ("x-tool <arg> | sort | head <n>", "x-tool <arg> | sort | head <n>"),
                           ("x-tool <arg> | head <n> | jq <str>", "x-tool <arg> | head <n> | jq <str>"),
                           ("bd show <arg>", "bd show <arg>"), (". .env", ". .env")):
            with self.subTest(step=step):
                self.assertEqual(hook.chore(step), want)


if __name__ == "__main__":
    unittest.main()
