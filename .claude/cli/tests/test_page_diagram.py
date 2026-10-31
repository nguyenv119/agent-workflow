"""Tests for the `diagram_wiring` page rule: an inline <svg> box needs an arrow ending at its edge, box text
must fit the box, and a legend swatch holds no text.

The fixtures under fixtures/page/ keep the geometry of a real page whose third layer added a row of boxes with
no arrows; their labels are generic. The rule is off by default, so each case turns it on.
"""
import json
import sys
import unittest
from pathlib import Path

from helpers import CLAUDE, TmpCase, agent_cli, make_project

sys.path.insert(0, str(CLAUDE / "cli"))
from agent_cli import page  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "page"
LOOSE = (FIXTURES / "diagram_loose.html").read_text()
WIRED = (FIXTURES / "diagram_wired.html").read_text()
LEGEND_TEXT = (FIXTURES / "diagram_legend_text.html").read_text()
LINE = '<line class="ar" x1="{}" y1="{}" x2="{}" y2="{}" marker-end="url(#a1)"/>'


def check(html, **wiring):
    r = page.load_rules()[0]
    r["diagram_wiring"] = {**r["diagram_wiring"], "on": True, **wiring}
    return page.check_page(html, r)


class DiagramWiring(unittest.TestCase):
    def test_boxes_with_no_arrow_are_named_with_their_layer(self):
        """Each box no arrow touches is reported by its label and its layer, so the author knows which to wire.

        Why: a page grew a row of boxes with no arrows and nobody saw it until reading the picture by eye.
        Without this the picture says 'these things relate' where nothing does.
        """
        # GIVEN a layer whose last row of three boxes has no arrow; WHEN checked with the rule on
        out = check(LOOSE)
        # THEN exactly those three are loose, each named with the layer
        loose = [v for v in out if "loose box" in v]
        self.assertEqual(len(out), 3, out)
        for name in ("LOOSE ONE", "LOOSE TWO", "LOOSE THREE"):
            self.assertTrue(any(f"'Layer 3. Most items stop before the third step': loose box '{name}'" in v
                                for v in loose), out)

    def test_wired_diagram_passes(self):
        """The control: the same picture with an arrow into each new box has no violation, so the failure above
        is the missing arrows and nothing else (the 360 px box and the pills are not mistaken for loose)."""
        # GIVEN the loose fixture plus an arrow into each new box; WHEN checked; THEN nothing is reported
        self.assertEqual(check(WIRED), [])

    def test_rule_is_off_by_default(self):
        """A project that never turned the rule on sees no change: the loose fixture passes the built-in defaults."""
        # GIVEN the loose fixture; WHEN checked with the defaults; THEN it passes
        self.assertEqual(page.check_page(LOOSE, page.load_rules()[0]), [])

    def test_arrow_end_must_be_within_tolerance_of_the_edge(self):
        """An arrow that stops short of its box does not connect it; 4 px passes, 5 px fails (tolerance 4)."""
        # GIVEN a single box and one arrow ending `gap` px above it
        def one(gap):
            return WIRED.replace(LINE.format(95, 194, 95, 228), LINE.format(95, 194, 95, 230 - gap))
        # WHEN checked; THEN the arrow 4 px away still connects LOOSE ONE and 5 px does not
        self.assertEqual(check(one(4)), [])
        out = check(one(5))
        self.assertEqual(len(out), 1, out)
        self.assertIn("loose box 'LOOSE ONE'", out[0])

    def test_text_wider_than_its_box_fails(self):
        """A label or item line longer than the box (6.2 px a char, 7.2 for a label) is flagged with both widths.

        Why: SVG text does not wrap, so a long phrase spills over the next box and reads as part of it."""
        # GIVEN an item line of 30 chars (186 px) in a 170 px box and a 25-char label (180 px) in another
        out = check(WIRED.replace("first thing", "x" * 30).replace("LOOSE TWO", "L" * 25))
        # THEN both are reported against their own box
        self.assertTrue(any("text 'xxxx" in v and "186 px wide, the box 'STEP ONE' 170" in v for v in out), out)
        self.assertTrue(any("text 'LLLL" in v and "180 px wide, the box 'LLLL" in v for v in out), out)
        self.assertEqual(len(out), 2, out)

    def test_text_in_the_wide_box_fits_by_the_wide_box_width(self):
        """The 360 px box takes a 50-char line (310 px); width is the box's own, not a fixed column."""
        # GIVEN 50 chars in the 360 px box; WHEN checked; THEN it fits
        self.assertEqual(check(WIRED.replace("walks every item through", "y" * 50)), [])

    def test_centred_text_is_measured_from_its_middle(self):
        """text-anchor="middle" puts x at the centre, so a short centred line fits and a long one spills both ways."""
        # GIVEN the text of STEP TWO (box x 200..370) re-anchored at the box centre 285 with 28 chars (173.6 px)
        old = '<text class="it" x="212" y="72">second thing</text>'
        new = '<text class="it" text-anchor="middle" x="285" y="72">{}</text>'
        self.assertIn(old, WIRED)
        # WHEN checked with a fitting line and then a 28-char line; THEN only the second is reported
        self.assertEqual(check(WIRED.replace(old, new.format("z" * 20))), [])
        self.assertEqual(len(check(WIRED.replace(old, new.format("z" * 28)))), 1)

    def test_legend_swatch_with_text_fails(self):
        """A swatch holding words is a 10 px square, so the words wrap one per line; the legend is flagged."""
        # GIVEN a legend whose swatch span holds ' update 2'; WHEN checked; THEN it is flagged
        out = check(LEGEND_TEXT)
        self.assertEqual(len(out), 1, out)
        self.assertIn("legend swatch holds text 'update 2'", out[0])

    def test_pill_and_badge_rects_need_no_arrow(self):
        """A rect under min_height (a PR pill, a work badge) is not a box and needs no arrow."""
        # GIVEN a 50x18 pill rect with text inside, on the wired fixture; WHEN checked; THEN it is not loose
        pill = '<rect class="bx" x="120" y="21" width="50" height="18" rx="9"/><text class="lb" x="127" y="34">#3184</text>'
        self.assertEqual(check(WIRED.replace("</svg>", pill + "</svg>")), [])


class RulesFile(TmpCase):
    def run_check(self, rules, html):
        p = make_project(self.tmp / "proj")
        (self.tmp / "rules.json").write_text(json.dumps(rules))
        (self.tmp / "page.html").write_text(html)
        return agent_cli(p, "page", "check", str(self.tmp / "page.html"), "--rules", str(self.tmp / "rules.json"))

    def test_rules_file_turns_the_rule_on_and_checks_its_keys(self):
        """A rules file switches the rule on with {"diagram_wiring": {"on": true}}; a misspelt key is refused, not ignored."""
        # GIVEN rules turning it on; WHEN `page check` runs on the loose fixture; THEN exit 1 naming a loose box
        r = self.run_check({"diagram_wiring": {"on": True}}, LOOSE)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("loose box 'LOOSE ONE'", r.stdout)
        # AND the wired fixture passes
        self.assertEqual(self.run_check({"diagram_wiring": {"on": True}}, WIRED).stdout.strip(), "PASS")
        # AND a misspelt key exits 2
        r = self.run_check({"diagram_wiring": {"enabled": True}}, LOOSE)
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn("unknown key diagram_wiring.enabled", r.stderr)


if __name__ == "__main__":
    unittest.main()
