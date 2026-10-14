"""Tests for `agent-cli card check|write` and per-field card rules.

Each field rule gets a planted card that breaks it (must fail) next to the good card (must pass). The default
rules (needs-you fields, tracker ids, PR numbers, tokens, em dashes) are covered by
harness-loop/tests/test_check_page.py through the check_page.py shim.
"""
import datetime
import json
import sys
import unittest
from pathlib import Path

from helpers import CLAUDE, TmpCase, agent_cli, make_project

sys.path.insert(0, str(CLAUDE / "cli"))
from agent_cli import card  # noqa: E402

GOOD = {
    "id": "cache-size", "area": "Speed", "loop": "make the app fast", "piece": "cache size",
    "status": "needs-you", "title": "Raise the cache to 10,000 entries?",
    "background": "The app keeps recent answers in memory.", "why": "Half the requests recompute.",
    "choice": "Keep 1,000, or raise to 10,000 for 40 MB.", "recommendation": "Raise it.",
    "detail": "PAGE (layer 2)", "updated": "2026-10-04", "rank": 2,
}
RULES = {
    "id_is_filename": True, "id_pattern": "^[a-z0-9][a-z0-9-]*$", "required": ["id", "status"],
    "fields": {
        "status": {"enum": ["needs-you"]}, "area": {"enum": ["Speed", "Cost"]},
        "loop": {"words": [3, 8]}, "piece": {"words": [2, 6]}, "title": {"pattern": r"\?$"},
        "updated": {"date": True}, "rank": {"type": "int", "enum": [1, 2, 3]},
        "detail": {"path": "file", "ext": [".html"]}, "figure": {"path": "file", "base": "figs", "palette": ["#111111"]},
        "folder": {"path": "dir"}, "source": {"path": "any"},
        "depends_on": {"type": "list", "refs": "same:area"},
    },
    "no_emails": True, "cleared_log": {"path": "cleared.log"}, "stamp_today": ["updated"],
}


class Fields(TmpCase):
    def setUp(self):
        super().setUp()
        self.cards = self.tmp / "cards"
        self.cards.mkdir()
        (self.tmp / "figs").mkdir()
        (self.tmp / "figs/ok.svg").write_text('<svg><rect fill="#111111"/></svg>')
        (self.tmp / "figs/loud.svg").write_text('<svg><rect fill="#ff0000"/></svg>')
        self.page = self.tmp / "page.html"
        self.page.write_text("<html></html>")
        (self.tmp / "cleared.log").write_text("2026-10-03T10:00:00 old-one cleared by x: gone?\n")
        self.rules_file = self.tmp / "card-rules.json"
        self.rules_file.write_text(json.dumps(RULES))
        self.rules = card.load_rules(str(self.rules_file))[0]

    def good(self, **over):
        c = {**GOOD, "detail": f"{self.page} (layer 2)", **over}
        return {k: v for k, v in c.items() if v is not None}

    def check(self, *cards, rules=None):
        for f in self.cards.glob("*.json"):
            f.unlink()
        for c in cards:
            name = c.pop("_file", None) or f"{c['id']}.json"
            (self.cards / name).write_text(json.dumps(c))
        return card.check_cards(str(self.cards), rules=rules or self.rules, base=self.tmp)

    def test_good_card_passes(self):
        """The control: a card that keeps every field rule passes, so each failure below is the rule's own."""
        # GIVEN a card that fills every rule, a figure in the palette, an existing folder and path
        c = self.good(figure="ok.svg", folder=str(self.tmp / "figs"), source=str(self.page))
        # WHEN the folder is checked; THEN nothing is reported
        self.assertEqual(self.check(c), [])

    def test_each_planted_break_fails_with_its_own_message(self):
        """One planted break per field rule on an otherwise good card; the message names the field and the rule,
        so a reader knows which field to fix."""
        # GIVEN one bad card per rule
        cases = {
            "must equal the file name": self.good(_file="other.json"),
            "does not match '^[a-z0-9]": self.good(id="Cache_Size", _file="Cache_Size.json"),
            "status: 'running' is not one of needs-you": self.good(status="running"),
            "area: 'Speedy' is not one of": self.good(area="Speedy"),
            "loop: 2 words, want 3 to 8": self.good(loop="go fast"),
            "piece: 7 words, want 2 to 6": self.good(piece="one two three four five six seven"),
            "title: does not match": self.good(title="Raise the cache."),
            "updated: '4 Oct' is not a YYYY-MM-DD date": self.good(updated="4 Oct"),
            "rank: 5 is not one of": self.good(rank=5),
            "rank: must be a int": self.good(rank="1"),
            "is not an existing file": self.good(detail=str(self.tmp / "gone.html")),
            "must be an absolute path": self.good(detail="page.html"),
            "must end in .html": self.good(detail=str(self.rules_file)),
            "colours outside the palette: #ff0000": self.good(figure="loud.svg"),
            "folder: '%s' is not an existing dir" % self.page: self.good(folder=str(self.page)),
            "source: '%s' is not an existing path" % (self.tmp / "gone"): self.good(source=str(self.tmp / "gone")),
            "'nope' is not a card in this folder": self.good(depends_on=["nope"]),
            "email address": self.good(why="Ask " + "someone" + "@" + "example.com" + " first."),
            "was cleared on 2026-10-03": self.good(id="old-one"),
            "status: missing": self.good(status=None),
        }
        for want, c in cases.items():
            with self.subTest(want):
                # WHEN checked alone; THEN its own message is among the violations
                out = self.check(c)
                self.assertTrue(any(want in v for v in out), out)

    def test_depends_on_a_card_in_another_area_fails(self):
        """A dependency across areas would split one decision over two lists; the reader sees which ref is wrong."""
        # GIVEN card b depends on a, which sits in another area
        cards = (self.good(id="a", area="Cost"), self.good(id="b", depends_on=["a"]))
        # WHEN checked; THEN the ref is flagged
        out = self.check(*cards)
        self.assertTrue(any("'a' has a different area" in v for v in out), out)

    def test_depends_on_a_card_in_the_same_area_passes(self):
        """The control for the refs rule: a dependency inside one area is fine."""
        # GIVEN b depends on a in the same area; WHEN checked; THEN nothing is reported
        self.assertEqual(self.check(self.good(id="a"), self.good(id="b", depends_on=["a"])), [])

    def test_reopened_on_or_after_the_clear_passes(self):
        """A cleared id may come back when the reader reopens it, dated on or after the clear."""
        # GIVEN a cleared id with a later reopened date; WHEN checked; THEN it passes
        self.assertEqual(self.check(self.good(id="old-one", reopened="2026-10-04")), [])

    def test_reopened_before_the_clear_fails(self):
        """A reopened date older than the clear does not count: the card was cleared after it."""
        # GIVEN a cleared id reopened before its clear; WHEN checked; THEN it still counts as cleared
        out = self.check(self.good(id="old-one", reopened="2026-10-01"))
        self.assertTrue(any("was cleared" in v for v in out), out)

    def test_cleared_log_pattern_from_the_rules_is_used(self):
        """A project whose log has another line shape names it with cleared_log.pattern; the default would miss it."""
        # GIVEN a log in a custom shape and rules that describe it
        (self.tmp / "custom.log").write_text("2026-10-03 id=old-one\n")
        r = {**self.rules, "cleared_log": {"path": "custom.log", "pattern": r"^(\S+) id=(\S+)"}}
        # WHEN the cleared id comes back; THEN it is caught
        out = self.check(self.good(id="old-one"), rules=r)
        self.assertTrue(any("was cleared on 2026-10-03" in v for v in out), out)

    def test_needs_you_field_that_is_not_text_fails_under_the_defaults(self):
        """The decision fields are prose for a reader; a number or list there is a broken card."""
        # GIVEN a needs-you card whose title is a number
        c = {**GOOD, "title": 5}
        # WHEN checked with the built-in defaults; THEN the title is flagged
        out = self.check(c, rules=card.DEFAULTS)
        self.assertIn("cache-size.json: title: must be text", out)


class Write(TmpCase):
    def setUp(self):
        super().setUp()
        self.p = make_project(self.tmp / "proj")
        self.cards = self.tmp / "cards"
        self.cards.mkdir()
        self.page = self.tmp / "page.html"
        self.page.write_text("<html></html>")
        (self.tmp / "cleared.log").write_text("2026-10-03T10:00:00 old-one cleared by x: gone?\n")
        self.rules = self.tmp / "card-rules.json"
        self.rules.write_text(json.dumps(RULES))

    def card(self, **over):
        return {**GOOD, "detail": str(self.page), **over}

    def write(self, c):
        return agent_cli(self.p, "card", "write", str(self.cards), "--rules", str(self.rules), "--file", "-",
                         stdin=json.dumps(c))

    def test_good_card_is_written_with_today_stamped(self):
        """write stamps `updated` with today and writes <id>.json, so an agent never types the date."""
        # GIVEN a good card with an old date; WHEN written
        r = self.write(self.card(updated="2020-01-01"))
        # THEN the file holds today's date
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        saved = json.loads((self.cards / "cache-size.json").read_text())
        self.assertEqual(saved["updated"], datetime.date.today().isoformat())

    def test_written_card_passes_check(self):
        """What write accepts, check accepts: the two commands share one rule set."""
        # GIVEN a card written by `card write`
        self.write(self.card())
        # WHEN the folder is checked; THEN it passes
        r = agent_cli(self.p, "card", "check", str(self.cards), "--rules", str(self.rules))
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "PASS"))

    def test_bad_card_is_not_written(self):
        """A card with a violation prints it, exits 1 and leaves the folder as it was."""
        # GIVEN a title without a question mark; WHEN written
        r = self.write(self.card(title="No question mark"))
        # THEN it is refused and nothing lands
        self.assertEqual(r.returncode, 1)
        self.assertIn("title: does not match", r.stdout)
        self.assertEqual(list(self.cards.iterdir()), [])

    def test_cleared_id_is_not_written_back(self):
        """write reads the cleared log, so an agent cannot bring back a card the reader already answered."""
        # GIVEN old-one in the cleared log; WHEN a card with that id is written
        r = self.write(self.card(id="old-one"))
        # THEN it is refused and nothing lands
        self.assertEqual(r.returncode, 1)
        self.assertIn("was cleared on 2026-10-03", r.stdout)
        self.assertEqual(list(self.cards.iterdir()), [])

    def test_dependency_on_a_card_already_in_the_folder_is_written(self):
        """write checks refs against the folder's cards, so a new card may depend on one already there."""
        # GIVEN card a in the folder
        self.assertEqual(self.write(self.card(id="a")).returncode, 0)
        # WHEN b depending on a is written; THEN it lands
        r = self.write(self.card(id="b", depends_on=["a"]))
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertTrue((self.cards / "b.json").exists())

    def test_dependency_on_a_missing_card_is_not_written(self):
        """A ref to a card that is not in the folder is refused at write time."""
        # GIVEN an empty folder; WHEN b depending on a is written; THEN it is refused
        r = self.write(self.card(id="b", depends_on=["a"]))
        self.assertEqual(r.returncode, 1)
        self.assertIn("'a' is not a card in this folder", r.stdout)

    def test_id_that_is_not_a_file_name_is_refused(self):
        """An id that would write outside the folder is a usage error (exit 2), and nothing is written."""
        # GIVEN an id with ../; WHEN written; THEN exit 2 and no file outside the folder
        r = self.write(self.card(id="../escape"))
        self.assertEqual(r.returncode, 2)
        self.assertFalse(Path(self.tmp / "escape.json").exists())

    def test_new_card_file_is_readable_by_others(self):
        """A new card gets the usual file mode, not the owner-only mode of a temp file, so a server can read it."""
        # GIVEN no card; WHEN one is written
        self.write(self.card())
        # THEN group and others may read it (umask permitting)
        self.assertTrue((self.cards / "cache-size.json").stat().st_mode & 0o044)

    def test_empty_folder_check_is_a_usage_error(self):
        """Checking a folder with no cards is almost always a wrong path, so it exits 2, not PASS."""
        # GIVEN an empty folder; WHEN checked; THEN exit 2
        r = agent_cli(self.p, "card", "check", str(self.cards))
        self.assertEqual(r.returncode, 2)


if __name__ == "__main__":
    unittest.main()
