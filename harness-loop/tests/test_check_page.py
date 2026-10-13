"""Tests for .claude/skills/document/check_page.py: the document page and to-do card format checker.

Format rules written as prose in the document skill kept getting broken, so the checkable ones live
in this script instead. Each rule gets one page (or card) that passes and one that fails. Pages and
cards are synthetic and built inline; token-shaped strings are assembled by concatenation so no
literal secret pattern sits in the repo.
"""
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / ".claude/skills/document/check_page.py"
_spec = importlib.util.spec_from_file_location("check_page", SCRIPT)
check_page = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_page)

EM = "—"
FIG = '<figure class="dia ent"><svg viewBox="0 0 10 10"><text>BOX</text></svg></figure>'


def layer(heading="Layer 1. The cache holds recent results", pain=True, fig=True, done=True, h3=True):
    """One div.layer; each flag drops one required part."""
    return ('<div class="layer">'
            + (f"<h3>{heading}</h3>" if h3 else "")
            + ('<p class="pain"><b>Original pain point:</b> every request recomputed the answer.</p>' if pain else "")
            + "<p>A cache keeps the last answer for each key.</p>"
            + (FIG if fig else "")
            + ('<p class="done"><b>Done:</b> the cache is built and halves the load.</p>' if done else "")
            + "</div>")


def page(layers=None, legend='<span class="new"><span class="sw"></span> update 2: cache layer</span>', extra=""):
    """A minimal page: hero legend, a Why section, How it works with the given layers."""
    layers = [layer()] if layers is None else layers
    return ("<html><head><style>p::before { content: '" + EM + "'; }</style></head><body>"
            f'<header class="hero"><h1>Cache for acme/app</h1><div class="legend">{legend}</div></header>'
            "<section class=\"block\"><h2>Why</h2><p>Requests are slow.</p></section>"
            '<section class="block"><h2>How it works, layer by layer</h2>' + "".join(layers) + "</section>"
            + extra + "</body></html>")


GOOD_CARD = {
    "id": "cache-size", "status": "needs-you", "title": "Pick a cache size for acme/app",
    "background": "The app keeps recent answers in memory so repeat requests are fast.",
    "why": "The current size evicts answers before they are reused, so half the requests recompute.",
    "choice": "Keep 1,000 entries, or raise it to 10,000 at about 40 MB more memory.",
    "recommendation": "Raise it to 10,000; memory is cheap next to the recompute time.",
    "detail": "/tmp/acme-app-cache-2026-01-01.html",
}


def check_cards(cards):
    """Write {filename: card} into a temp dir and run the card check on it."""
    with tempfile.TemporaryDirectory() as d:
        for name, card in cards.items():
            Path(d, name).write_text(card if isinstance(card, str) else json.dumps(card))
        return check_page.check_cards(d)


def card(**changes):
    """GOOD_CARD with some fields replaced."""
    return {**GOOD_CARD, **changes}


class PageRules(unittest.TestCase):
    def test_complete_page_passes(self):
        """A page whose every layer has h3, pain, diagram and done, with one swatch and no em dash,
        has no violations. If this fails, every real page would be blocked by a false positive."""
        # GIVEN a well-formed page with two layers
        html = page([layer(), layer("Layer 2. Misses go to the database")])
        # WHEN it is checked
        out = check_page.check_page(html)
        # THEN nothing is reported
        self.assertEqual(out, [])

    def test_layer_missing_a_part_is_named_by_its_heading(self):
        """Each layer must carry its pain point, diagram and done line; a missing one is reported with
        the layer's heading so the agent knows which layer to fix. Without this, layers drift into
        unstructured prose the reader cannot read cold."""
        for flag, part in (("pain", "p.pain"), ("fig", "figure.dia"), ("done", "p.done")):
            with self.subTest(part=part):
                # GIVEN a page whose second layer lacks one part
                html = page([layer(), layer("Layer 2. Misses go to the database", **{flag: False})])
                # WHEN it is checked
                out = check_page.check_page(html)
                # THEN exactly one violation names that layer and part
                self.assertEqual(len(out), 1, out)
                self.assertIn("Layer 2. Misses go to the database", out[0])
                self.assertIn(part, out[0])

    def test_layer_without_heading_is_reported(self):
        """A layer with no h3 has no name for the reader; it is reported by its position instead."""
        # GIVEN a page whose only layer has no h3
        html = page([layer(h3=False)])
        # WHEN it is checked
        out = check_page.check_page(html)
        # THEN the missing h3 is reported for layer 1
        self.assertEqual(len(out), 1, out)
        self.assertIn("layer 1", out[0])
        self.assertIn("h3", out[0])

    def test_two_legend_swatches_fail(self):
        """Only the latest update is highlighted, so the legend has one swatch; two means an old
        update's highlight was left behind and the reader cannot tell what is new."""
        # GIVEN a legend with two swatches
        legend = ('<span class="new"><span class="sw"></span> update 2</span>'
                  '<span class="new"><span class="sw"></span> update 3</span>')
        # WHEN it is checked
        out = check_page.check_page(page(legend=legend))
        # THEN one legend violation is reported
        self.assertEqual(len(out), 1, out)
        self.assertIn("legend", out[0])

    def test_em_dash_in_visible_text_fails_with_heading(self):
        """Page copy has no em dashes; one in visible text is reported with the nearest heading,
        including when written as an HTML entity."""
        for dash in (EM, "&mdash;", "&#8212;"):
            with self.subTest(dash=dash):
                # GIVEN a Why paragraph with an em dash
                html = page().replace("Requests are slow.", f"Requests are slow {dash} very slow.")
                # WHEN it is checked
                out = check_page.check_page(html)
                # THEN the violation names the Why heading
                self.assertEqual(len(out), 1, out)
                self.assertIn("Why", out[0])
                self.assertIn("em dash", out[0])

    def test_em_dash_in_style_script_or_comment_passes(self):
        """Em dashes the reader never sees (CSS, script, comments) are not copy and must not fail."""
        # GIVEN em dashes only in a script and a comment (page() already has one in <style>)
        extra = f"<script>var s = '{EM}';</script><!-- note {EM} for agents -->"
        # WHEN it is checked
        out = check_page.check_page(page(extra=extra))
        # THEN nothing is reported
        self.assertEqual(out, [])

    def test_layer_heading_outside_div_layer_fails(self):
        """Old pages have bare <h3>Layer N...</h3> headings with no div.layer, so the layer-part check
        never ran on them. Each such heading is one violation; other h3s are not layers."""
        # GIVEN a page with two bare layer headings and an unrelated h3
        bare = "<h3>Layer 1. Bare one</h3><p>text</p><h3>Layer 2. Bare two</h3><h3>Open question</h3>"
        # WHEN it is checked
        out = check_page.check_page(page([bare]))
        # THEN one line per bare layer heading
        self.assertEqual(len(out), 2, out)
        self.assertTrue(all("not wrapped in div.layer" in line for line in out), out)
        self.assertIn("Layer 2. Bare two", out[1])

    def test_old_update_and_todo_boxes_belong_in_for_agents(self):
        """div.upd and div.ltodo moved to For agents; one left on the main page fails, the same box
        inside details.agents passes."""
        for cls in ("upd", "ltodo"):
            with self.subTest(cls=cls):
                box = f'<div class="{cls}"><p>Found: old note.</p></div>'
                # GIVEN the box on the main page, and the box inside For agents
                main = page([layer(), box])
                agents = page(extra=f'<details class="agents"><summary>For agents</summary>'
                                    f"<details><summary>Earlier</summary>{box}</details>{box}</details>")
                # WHEN both are checked
                # THEN only the main-page box is reported
                out = check_page.check_page(main)
                self.assertEqual(len(out), 1, out)
                self.assertIn(f"div.{cls} outside For agents", out[0])
                self.assertEqual(check_page.check_page(agents), [])

    def test_unclosed_div_in_a_layer_does_not_hide_later_layers(self):
        """A stray unclosed <div> used to keep the parser inside layer 1 forever, so later layers
        were never checked. Layers never nest, so a new div.layer starts a new layer."""
        # GIVEN layer 1 with an unclosed div, then a layer 2 that lacks its done line
        broken = layer().replace("<p>A cache", "<div><p>A cache")
        html = page([broken, layer("Layer 2. Misses go to the database", done=False)])
        # WHEN it is checked
        out = check_page.check_page(html)
        # THEN layer 2's missing part is still reported
        self.assertEqual(len(out), 1, out)
        self.assertIn("Layer 2. Misses go to the database", out[0])
        self.assertIn("p.done", out[0])

    def test_unclosed_layer_before_a_bare_heading_is_reported(self):
        """A complete layer 1 left open swallows a later bare <h3>Layer 2</h3>, which would rename
        layer 1 and inherit its parts. A second h3 in one layer means its div was never closed."""
        # GIVEN a complete layer 1 with no closing div, then a bare layer 2 heading
        html = page([layer()[:-len("</div>")] + "<h3>Layer 2. Misses go to the database</h3>"])
        # WHEN it is checked
        out = check_page.check_page(html)
        # THEN the unclosed layer is reported before the layer 2 heading
        self.assertEqual(len(out), 1, out)
        self.assertIn("unclosed div.layer before 'Layer 2. Misses go to the database'", out[0])

    def test_layer_words_that_are_not_layer_headings_pass(self):
        """Old layer notes belong in For agents, and "Layers at a glance" is not a numbered layer;
        neither is a bare layer heading on the main page."""
        # GIVEN a numbered layer heading inside For agents and an unnumbered "Layers" h3 outside
        extra = ('<h3>Layers at a glance</h3><details class="agents"><summary>For agents</summary>'
                 "<h3>Layer 3. Old note from an earlier turn</h3></details>")
        # WHEN it is checked
        # THEN nothing is reported
        self.assertEqual(check_page.check_page(page(extra=extra)), [])

    def test_template_passes_its_own_checker(self):
        """Every new page starts as a copy of template.html; if the template fails, every new page
        fails before anyone writes a word."""
        # GIVEN the shipped template
        html = (SCRIPT.parent / "template.html").read_text(encoding="utf-8")
        # WHEN it is checked
        # THEN nothing is reported
        self.assertEqual(check_page.check_page(html), [])


class CardRules(unittest.TestCase):
    def test_complete_card_passes(self):
        """A needs-you card with every field filled and plain prose passes; a false positive here
        would block every card render."""
        # GIVEN one good card
        # WHEN the card dir is checked
        out = check_cards({"cache-size.json": GOOD_CARD})
        # THEN nothing is reported
        self.assertEqual(out, [])

    def test_needs_you_card_missing_a_field_fails(self):
        """A needs-you card asks the reader to decide, so it needs background, why, choice and a
        recommendation; a card missing one is a bare question they cannot answer cold."""
        for field in check_page.CARD_FIELDS:
            with self.subTest(field=field):
                # GIVEN a needs-you card with one field empty
                # WHEN it is checked
                out = check_cards({"cache-size.json": card(**{field: ""})})
                # THEN the file and field are named
                self.assertEqual(len(out), 1, out)
                self.assertIn("cache-size.json", out[0])
                self.assertIn(field, out[0])

    def test_card_not_needing_you_may_skip_fields(self):
        """Only needs-you cards must carry the decision fields; a status card need not."""
        # GIVEN a card with another status and no choice
        # WHEN it is checked
        out = check_cards({"status.json": card(status="in-progress", choice="")})
        # THEN nothing is reported
        self.assertEqual(out, [])

    def test_bead_id_in_prose_fails(self):
        """Tracker ids mean nothing to the reader; prose names the work in words."""
        for text in ("Waiting on acme-app-7k2 first.", "Blocked by web-x9f2.3 today.", "See ab3c.12 for it.",
                     "Then word-abcde.3 lands.", "After abcde.31 merges."):
            with self.subTest(text=text):
                # GIVEN a card whose why mentions a tracker id
                # WHEN it is checked
                out = check_cards({"c.json": card(why=text)})
                # THEN one bead-id violation names the why field
                self.assertEqual(len(out), 1, out)
                self.assertIn("why", out[0])
                self.assertIn("bead", out[0])

    def test_ordinary_hyphenated_words_pass(self):
        """Hyphenated English (and numbers like top-100) is not a tracker id; flagging it would make
        the check cry wolf on normal prose."""
        # GIVEN prose full of hyphenated words
        text = ("A low-risk follow-up: re-run the top-100 list, check the covid-19 and x-ray rows, "
                "use gpt-4o for the e-mail on 2026-01-01, version v1.2 of python3.12. In the mid-90s and "
                "late-1990s, test-e2e runs on-k8s for the top-10k with x-2fa and web3.0.")
        # WHEN it is checked
        out = check_cards({"c.json": card(background=text)})
        # THEN nothing is reported
        self.assertEqual(out, [])

    def test_prefix_flags_letter_only_ids(self):
        """A project's ids can be letter-only (acme-qwert), which looks like a word; --prefix names
        the project so those are caught without flagging every hyphenated word elsewhere."""
        # GIVEN a why naming a letter-only id
        c = {"c.json": card(why="Blocked on acme-qwert today.")}
        # WHEN it is checked with and without the prefix
        # THEN only the prefixed check reports it
        self.assertEqual(check_cards(c), [])
        with tempfile.TemporaryDirectory() as d:
            Path(d, "c.json").write_text(json.dumps(c["c.json"]))
            out = check_page.check_cards(d, ("acme",))
        self.assertEqual(len(out), 1, out)
        self.assertIn("acme-qwert", out[0])

    def test_detail_path_may_hold_id_and_pr_shapes(self):
        """detail is a path for agents, not prose; an id- or PR-shaped path must not fail."""
        # GIVEN a detail path with an id and a hash in it
        # WHEN it is checked
        out = check_cards({"c.json": card(detail="/tmp/acme-7k2.3/notes-ab3c.12#12.md")})
        # THEN nothing is reported
        self.assertEqual(out, [])

    def test_html_entity_and_word_hash_are_not_pr_numbers(self):
        """'&#123;' is an HTML entity and 'issue#5' is glued to a word; neither is a PR pointer."""
        # GIVEN prose with both
        # WHEN it is checked
        out = check_cards({"c.json": card(why="The brace &#123; and issue#5 are text.")})
        # THEN nothing is reported
        self.assertEqual(out, [])

    def test_pr_number_in_prose_fails(self):
        """PR numbers are pointers for agents, not words for the reader."""
        # GIVEN a recommendation naming a PR
        # WHEN it is checked
        out = check_cards({"c.json": card(recommendation="Merge #123 then raise it.")})
        # THEN one PR violation names the field
        self.assertEqual(len(out), 1, out)
        self.assertIn("recommendation", out[0])
        self.assertIn("PR", out[0])

    def test_token_shaped_string_fails(self):
        """A card is rendered into a page; a pasted key would leak. Values are built by
        concatenation so no real-looking secret is written in the repo."""
        fakes = {
            "sk": "sk-" + "x" * 24,
            "ghp": "ghp_" + "X" * 30,
            "akia": "AKIA" + "X" * 16,
            "hex": "ab" * 20,
            "base64": "Zm9v" * 6 + "QmFy" * 3,
        }
        for kind, fake in fakes.items():
            with self.subTest(kind=kind):
                # GIVEN a card whose detail holds a token-shaped string
                # WHEN it is checked
                out = check_cards({"c.json": card(detail=f"key {fake} here")})
                # THEN one token violation is reported, without echoing the whole value
                self.assertEqual(len(out), 1, out)
                self.assertIn("token", out[0])
                self.assertNotIn(fake, out[0])

    def test_dated_mixed_case_filenames_are_not_tokens(self):
        """Report filenames mix case and digits and run past 32 chars; - and _ split them, so they
        are not read as base64 keys."""
        for path in ("/tmp/Some-Report-Name-For-Client-2026-10-13.html",
                     "/tmp/Client_Intro_Packet_Final_Version_2026_10_08.pdf"):
            with self.subTest(path=path):
                # GIVEN a detail holding the path
                # WHEN it is checked
                # THEN nothing is reported
                self.assertEqual(check_cards({"c.json": card(detail=path)}), [])

    def test_em_dash_in_card_fails(self):
        """Card copy follows the same no-em-dash rule as pages."""
        # GIVEN a title with an em dash
        # WHEN it is checked
        out = check_cards({"c.json": card(title=f"Cache size {EM} pick one")})
        # THEN one violation names the title field
        self.assertEqual(len(out), 1, out)
        self.assertIn("title", out[0])
        self.assertIn("em dash", out[0])

    def test_invalid_json_is_reported(self):
        """A broken card file cannot be rendered; it is a violation, not a crash."""
        # GIVEN a file that is not JSON
        # WHEN it is checked
        out = check_cards({"broken.json": "{not json"})
        # THEN the file is named
        self.assertEqual(len(out), 1, out)
        self.assertIn("broken.json", out[0])


class Cli(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True)

    def test_clean_page_prints_pass_and_exits_zero(self):
        """Agents run the script after each edit and read the exit code; PASS/0 means go on."""
        # GIVEN a clean page on disk
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, "p.html")
            p.write_text(page())
            # WHEN the script runs on it
            r = self.run_cli(str(p))
        # THEN it prints PASS and exits 0
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "PASS"))

    def test_bad_cards_print_one_line_each_and_exit_one(self):
        """Violations print one per line and exit 1 so a render step can stop on them."""
        # GIVEN two bad cards
        with tempfile.TemporaryDirectory() as d:
            Path(d, "a.json").write_text(json.dumps(card(why="")))
            Path(d, "b.json").write_text(json.dumps(card(title="Merge #77")))
            # WHEN the card check runs
            r = self.run_cli("cards", d)
        # THEN two lines and exit 1
        self.assertEqual(r.returncode, 1)
        self.assertEqual(len(r.stdout.strip().splitlines()), 2, r.stdout)

    def test_missing_path_is_a_usage_error(self):
        """A wrong path must not print PASS; it exits 2 so a typo is not read as a clean check."""
        # GIVEN a path that does not exist
        # WHEN the script runs on it
        r = self.run_cli("cards", "/nonexistent/cards-dir")
        # THEN it exits 2 without PASS
        self.assertEqual(r.returncode, 2)
        self.assertNotIn("PASS", r.stdout)

    def test_empty_card_folder_is_not_a_pass(self):
        """A folder with no cards means a wrong path or a broken export; PASS would hide it."""
        # GIVEN an empty folder
        with tempfile.TemporaryDirectory() as d:
            # WHEN the card check runs on it
            r = self.run_cli("cards", d)
        # THEN it exits 2 with a no-cards message
        self.assertEqual(r.returncode, 2)
        self.assertIn("no cards found", r.stderr)
        self.assertNotIn("PASS", r.stdout)

    def test_prefix_flag_on_the_cli(self):
        """--prefix reaches the card check from the command line."""
        # GIVEN a card naming a letter-only id
        with tempfile.TemporaryDirectory() as d:
            Path(d, "c.json").write_text(json.dumps(card(why="Blocked on acme-qwert.")))
            # WHEN the card check runs with --prefix acme
            r = self.run_cli("cards", d, "--prefix", "acme")
        # THEN it fails on that id
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("acme-qwert", r.stdout)


if __name__ == "__main__":
    unittest.main()
