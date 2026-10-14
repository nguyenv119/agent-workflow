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

    def check(self, *cards):
        for c in cards:
            name = c.pop("_file", None) or f"{c['id']}.json"
            (self.cards / name).write_text(json.dumps(c))
        return card.check_cards(str(self.cards), rules=self.rules, base=self.tmp)

    def test_good_card_passes(self):
        """The control: a card that keeps every field rule passes, so each failure below is the rule's own."""
        self.assertEqual(self.check(self.good(figure="ok.svg")), [])

    def test_each_planted_break_fails_with_its_own_message(self):
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
            "does not exist (want a file)": self.good(detail=str(self.tmp / "gone.html")),
            "must be an absolute path": self.good(detail="page.html"),
            "must end in .html": self.good(detail=str(self.rules_file)),
            "colours outside the palette: #ff0000": self.good(figure="loud.svg"),
            "'nope' is not a card in this folder": self.good(depends_on=["nope"]),
            "email address": self.good(why="Ask " + "someone" + "@" + "example.com" + " first."),
            "was cleared on 2026-10-03": self.good(id="old-one"),
            "status: missing": self.good(status=None),
        }
        for want, c in cases.items():
            with self.subTest(want):
                for f in self.cards.glob("*.json"):
                    f.unlink()
                out = self.check(c)
                self.assertTrue(any(want in v for v in out), out)

    def test_depends_on_must_stay_in_the_same_area(self):
        out = self.check(self.good(id="a", area="Cost"), self.good(id="b", depends_on=["a"]))
        self.assertTrue(any("'a' has a different area" in v for v in out), out)
        for f in self.cards.glob("*.json"):
            f.unlink()
        self.assertEqual(self.check(self.good(id="a"), self.good(id="b", depends_on=["a"])), [])

    def test_reopened_after_the_clear_passes(self):
        """A cleared id may come back only with a reopened date on or after the clear."""
        self.assertEqual(self.check(self.good(id="old-one", reopened="2026-10-04")), [])
        for f in self.cards.glob("*.json"):
            f.unlink()
        out = self.check(self.good(id="old-one", reopened="2026-10-01"))
        self.assertTrue(any("was cleared" in v for v in out), out)


class Write(TmpCase):
    def setUp(self):
        super().setUp()
        self.p = make_project(self.tmp / "proj")
        self.cards = self.tmp / "cards"
        self.cards.mkdir()
        self.page = self.tmp / "page.html"
        self.page.write_text("<html></html>")
        self.rules = self.tmp / "card-rules.json"
        self.rules.write_text(json.dumps({k: v for k, v in RULES.items() if k != "cleared_log"}))

    def write(self, c, *extra):
        return agent_cli(self.p, "card", "write", str(self.cards), "--rules", str(self.rules), "--file", "-",
                         *extra, stdin=json.dumps(c))

    def test_good_card_is_written_with_today_stamped(self):
        """write checks, stamps `updated` with today and writes <id>.json; check then passes on the folder."""
        r = self.write({**GOOD, "detail": str(self.page), "updated": "2020-01-01"})
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        saved = json.loads((self.cards / "cache-size.json").read_text())
        self.assertEqual(saved["updated"], datetime.date.today().isoformat())
        r = agent_cli(self.p, "card", "check", str(self.cards), "--rules", str(self.rules))
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "PASS"))

    def test_bad_card_is_not_written(self):
        """A card with a violation prints it, exits 1 and leaves the folder as it was."""
        r = self.write({**GOOD, "detail": str(self.page), "title": "No question mark"})
        self.assertEqual(r.returncode, 1)
        self.assertIn("title: does not match", r.stdout)
        self.assertEqual(list(self.cards.iterdir()), [])

    def test_id_that_is_not_a_file_name_is_refused(self):
        r = self.write({**GOOD, "id": "../escape"})
        self.assertEqual(r.returncode, 2)
        self.assertFalse(Path(self.tmp / "escape.json").exists())

    def test_empty_folder_check_is_a_usage_error(self):
        r = agent_cli(self.p, "card", "check", str(self.cards))
        self.assertEqual(r.returncode, 2)


if __name__ == "__main__":
    unittest.main()
