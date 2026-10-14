"""Tests for `agent-cli page check|mark-update|new-layer|new` and the rules file they read.

Each rule a rules file can turn on gets a planted page that breaks it (must fail) and one that keeps it (must
pass), so a rule cut from a skill's prose is proved to be enforced. The defaults are covered by
harness-loop/tests/test_check_page.py through the check_page.py shim.
"""
import json
import os
import sys
import unittest

from helpers import CLAUDE, TmpCase, agent_cli, make_project

sys.path.insert(0, str(CLAUDE / "cli"))
from agent_cli import page  # noqa: E402

FIG = '<figure class="dia"><svg viewBox="0 0 10 10"><text>BOX</text></svg></figure>'
PAIN = '<p class="pain"><b>Original pain point:</b> every request recomputed the answer.</p>'
DONE = '<p class="done"><b>Done:</b> the cache is built.</p>'
STRICT = {
    "hidden_required": True, "hidden_closed": True,
    "layer": {"ordered": True, "lead": {"p.pain": r"Original pain point\b", "p.done": r"Done\b"}},
    "legend": {"line": r"update \d+, \d{1,2} [A-Z][a-z]{2}: \S"},
    "kicker": {"selector": "p.kicker", "pattern": r"^Draft \d+ · .+ · working document"},
    "require_update_mark": True, "legacy_classes": r"u\d+", "unique_ids": True,
    "forbid_headings_outside_hidden": [{"pattern": "^(Open questions|Backlog)", "why": "belongs on the to-do list"}],
    "require_source": [{"pattern": "contenteditable", "why": "page lost the edit shell"}],
}


def layer(title="Layer 1. The cache holds recent results", body=None, cls="layer"):
    body = body if body is not None else PAIN + "<p>A cache keeps answers.</p>" + FIG + DONE
    return f'<div class="{cls}"><h3>{title}</h3>{body}</div>'


def doc(layers=None, legend='<span class="new"><span class="sw"></span> update 2, 4 Oct: cache layer</span>',
        kicker="Draft 3 &middot; 4 Oct 2026 &middot; owner A &middot; working document", agents="<details class=\"agents\">"
        "<summary>For agents</summary><p>notes</p></details>", extra='<p class="new">fresh text</p>', head=""):
    layers = [layer()] if layers is None else layers
    return (f"<html><head><style>p {{ color: red; }}{head}</style></head><body contenteditable>"
            f'<p class="kicker">{kicker}</p><div class="legend">{legend}</div>'
            f"<h2>Why</h2>{extra}<h2>How it works</h2>{''.join(layers)}{agents}</body></html>")


def rules(**over):
    r = page.load_rules()[0]
    for k, v in {**STRICT, **over}.items():
        if isinstance(v, dict) and isinstance(r.get(k), dict):
            r[k] = {**r[k], **v}
        else:
            r[k] = v
    return r


class StrictRules(unittest.TestCase):
    def check(self, html, **over):
        return page.check_page(html, rules(**over))

    def test_good_page_passes_every_strict_rule(self):
        """The control: a page that keeps every rule has no violations, so each failure below is the rule's own."""
        # GIVEN a page built to the strict rules; WHEN checked; THEN nothing is reported
        self.assertEqual(self.check(doc()), [])

    def test_each_planted_break_fails_with_its_own_message(self):
        """One planted break per rule, each on an otherwise good page; the message names the rule."""
        # GIVEN one bad page per rule
        cases = {
            "parts out of order": doc([layer(body=FIG + PAIN + "<p>x</p>" + DONE)]),
            "p.pain must start with /Original pain point": doc([layer(body='<p class="pain">pain</p><p>x</p>' + FIG + DONE)]),
            "p.done must start with /Done": doc([layer(body=PAIN + FIG + '<p class="done">built</p>')]),
            "does not match": doc(legend='<span class="new"><span class="sw"></span> latest: cache layer</span>'),
            "no element outside the legend": doc(extra="<p>old text</p>"),
            "kicker: 'Draft 3 · owner A'": doc(kicker="Draft 3 &middot; owner A"),
            "kicker: no p.kicker": doc().replace('class="kicker"', 'class="intro"'),
            "no details.agents section": doc(agents=""),
            "it must start closed": doc(agents='<details class="agents" open><p>notes</p></details>'),
            "class 'u3' is a legacy update class": doc(extra='<p class="u3 new">old colour</p>'),
            "id 'arr' is used 2 times": doc(extra='<p class="new">x</p><svg><marker id="arr"/></svg>'
                                                   '<svg><marker id="arr"/></svg>'),
            "'Open questions': belongs on the to-do list": doc(extra='<p class="new">x</p><h2>Open questions</h2>'),
            "page lost the edit shell": doc().replace(" contenteditable", ""),
        }
        for want, html in cases.items():
            with self.subTest(want):
                # WHEN checked; THEN its own message is among the violations
                out = self.check(html)
                self.assertTrue(any(want in v for v in out), out)

    def test_lead_is_a_regex_matched_at_the_start(self):
        """A lead names how a part must open, so a longer opening such as "Original pain point (ours):" passes."""
        # GIVEN a pain point whose lead runs on past the regex
        body = '<p class="pain"><b>Original pain point (ours):</b> slow.</p><p>x</p>' + FIG + DONE
        # WHEN checked; THEN no lead violation
        self.assertEqual(self.check(doc([layer(body=body)])), [])

    def test_hidden_section_may_hold_forbidden_headings_and_upd_boxes(self):
        """Inside the agents section, Open questions and old update boxes are allowed: that is where they go."""
        # GIVEN those inside details.agents
        agents = ('<details class="agents"><summary>For agents</summary><h2>Open questions</h2>'
                  '<div class="upd">old</div></details>')
        # WHEN checked; THEN no violation
        self.assertEqual(self.check(doc(agents=agents)), [])

    def test_unfilled_skeleton_text_fails(self):
        """A FILL: placeholder left in visible text fails, so a new-layer skeleton cannot ship half done."""
        # GIVEN a layer with FILL text; WHEN checked; THEN it is flagged
        out = self.check(doc([layer(body=PAIN + "<p>FILL: what it is</p>" + FIG + DONE)]))
        self.assertTrue(any("unfilled skeleton text" in v for v in out), out)


class MarkUpdate(TmpCase):
    def setUp(self):
        super().setUp()
        self.p = make_project(self.tmp / "proj")
        self.rules = self.tmp / "rules.json"
        self.rules.write_text(json.dumps({**STRICT, "update_css": {"needs": r"\.new\s*\{", "css": ".new { color: blue; }"}}))
        self.page = self.tmp / "cache-2026-10-04.html"

    def run_cli(self, *args):
        return agent_cli(self.p, "page", *args, "--rules", str(self.rules))

    def mark(self, html, *extra):
        self.page.write_text(html)
        r = self.run_cli("mark-update", str(self.page), "--note", "layer 2 covers <eviction>", "--date", "14 Oct",
                         "--json", *extra)
        return r, self.page.read_text()

    def test_old_marks_and_lines_go_and_draft_and_update_count_up(self):
        """mark-update strips every old mark (unwrapping mark-only spans), drops old legend lines and legacy
        classes, bumps the draft and writes update N+1, so only this update stands out."""
        # GIVEN a page with marked text, a legacy class and two legend lines
        legend = ('<span class="u1"><span class="sw"></span> update 1, 2 Oct: a</span>'
                  '<span class="new"><span class="sw"></span> update 2, 4 Oct: b</span>')
        # WHEN mark-update runs
        r, html = self.mark(doc(legend=legend, extra='<p>keep <span class="new">this</span></p>'
                                                     '<p class="new u2 big">marked</p>'))
        # THEN it reports the new numbers and the page holds one new line and no old marks
        self.assertEqual(r.returncode, 0, r.stderr)
        info = json.loads(r.stdout)
        self.assertEqual((info["draft"], info["update"]), (4, 3))
        self.assertIn("<p>keep this</p>", html)
        self.assertIn('<p class="big">marked</p>', html)
        self.assertIn("update 3, 14 Oct: layer 2 covers &lt;eviction&gt;", html)
        self.assertNotIn("update 2", html)
        self.assertIn("Draft 4 ", html)

    def test_page_fails_check_until_the_new_text_is_marked(self):
        """After mark-update nothing outside the legend is marked, so check fails until the agent marks its text."""
        # GIVEN a page just through mark-update
        self.mark(doc())
        # WHEN checked; THEN the missing mark is reported
        out = self.run_cli("check", str(self.page))
        self.assertEqual(out.returncode, 1)
        self.assertIn("no element outside the legend", out.stdout)

    def test_update_css_is_added_once(self):
        """The update css goes in when the page lacks it and is not added again when `needs` already matches."""
        # GIVEN a page without the css; WHEN marked twice
        r1, _ = self.mark(doc())
        r2, html = self.mark(self.page.read_text())
        # THEN the first run added it, the second did not
        self.assertEqual((json.loads(r1.stdout).get("css_added"), json.loads(r2.stdout).get("css_added")), (True, None))
        self.assertEqual(html.count(".new { color: blue; }"), 1)

    def test_swatch_on_the_line_itself_is_replaced_and_counted(self):
        """Some pages put the swatch class on the line's own span; that line is replaced and its number counts."""
        # GIVEN a legend line whose outer span carries the swatch and a nested span in its text
        legend = '<span class="sw"> update 5, 11 Oct: a <span class="k">key</span> point</span>'
        # WHEN marked
        r, html = self.mark(doc(legend=legend))
        # THEN update 6 replaces it and the page has one swatch
        self.assertEqual(json.loads(r.stdout)["update"], 6)
        self.assertNotIn("update 5", html)
        self.assertEqual(page.parse(html, rules()).swatches, 1)

    def test_legend_shape_it_cannot_clear_is_refused_and_page_untouched(self):
        """If old lines survive the edit the page would carry two swatches; it refuses rather than write that."""
        # GIVEN update lines nested in a div the legend matcher cannot see past
        legend = ('<div><span class="sw"></span> update 1, 1 Oct: a</div>'
                  '<div><span class="sw"></span> update 2, 2 Oct: b</div>')
        before = doc(legend=legend)
        # WHEN marked; THEN exit 2 and the file is as it was
        r, html = self.mark(before)
        self.assertEqual(r.returncode, 2)
        self.assertIn("remove them by hand", r.stderr)
        self.assertEqual(html, before)

    def test_mark_span_with_other_attributes_keeps_them(self):
        """Only a span whose sole attribute was the mark is unwrapped; an id after the class survives."""
        # GIVEN a marked span with an id; WHEN marked; THEN the span stays with its id
        _, html = self.mark(doc(extra='<p class="new">x <span class="new" id="keep">b</span></p>'))
        self.assertIn('<span id="keep">b</span>', html)

    def test_scripts_and_comments_are_left_alone(self):
        """class="new" inside a script string or a comment is code or guidance, not a mark."""
        # GIVEN both on the page; WHEN marked; THEN both are unchanged
        _, html = self.mark(doc(extra='<p class="new">x</p><!-- mark with class="new" -->'
                                      '<script>el.className = \'<b class="new">\';</script>'))
        self.assertIn('<!-- mark with class="new" -->', html)
        self.assertIn('<b class="new">', html)

    def test_note_with_a_forbidden_string_is_refused_and_page_untouched(self):
        """A note carrying an em dash would plant a violation in the legend; it is refused before any write."""
        # GIVEN a good page; WHEN the note has an em dash
        self.page.write_text(doc())
        before = self.page.read_text()
        r = self.run_cli("mark-update", str(self.page), "--note", "a \u2014 b")
        # THEN exit 2 and the file is as it was
        self.assertEqual(r.returncode, 2)
        self.assertEqual(self.page.read_text(), before)

    def test_page_without_legend_is_refused(self):
        """There is nowhere to write the update line, so it says so instead of guessing a place."""
        # GIVEN a page with no legend; WHEN marked; THEN exit 2 naming the legend
        self.page.write_text("<html><body><p>no legend</p></body></html>")
        r = self.run_cli("mark-update", str(self.page), "--note", "x")
        self.assertEqual(r.returncode, 2)
        self.assertIn("legend", r.stderr)


class NewLayer(TmpCase):
    def setUp(self):
        super().setUp()
        self.p = make_project(self.tmp / "proj")
        self.page = self.tmp / "p.html"
        self.page.write_text(doc([layer("Layer 1. One"), layer("Layer 2. Two")]))

    def add(self, title, *extra):
        return agent_cli(self.p, "page", "new-layer", str(self.page), "--title", title, *extra)

    def test_layer_goes_after_the_named_layer_or_the_last(self):
        """--after picks the layer to follow; without it the new layer goes last, so a zoom-in sits by its parent."""
        # GIVEN two layers; WHEN one goes after layer 1 and one goes last
        r1 = self.add("Layer 1a. Zoom", "--after", "Layer 1.")
        r2 = self.add("Layer 3. Three")
        # THEN the order is 1, 1a, 2, 3
        self.assertEqual((r1.returncode, r2.returncode), (0, 0), r1.stderr + r2.stderr)
        html = self.page.read_text()
        order = [html.index(t) for t in ("Layer 1. One", "Layer 1a. Zoom", "Layer 2. Two", "Layer 3. Three")]
        self.assertEqual(order, sorted(order))

    def test_skeleton_has_every_part_marked_new_and_fails_until_filled(self):
        """The skeleton carries every part, marked as the update, and its FILL text fails check until replaced."""
        # GIVEN a page; WHEN a layer is added
        self.add("Layer 3. Three")
        # THEN it is marked, has every part, and fails only on the FILL lines
        html = self.page.read_text()
        out = page.check_page(html, page.load_rules()[0])
        self.assertEqual(html.count('class="layer new"'), 1)
        self.assertFalse(any("lacks" in v for v in out), out)
        self.assertEqual(sum("unfilled skeleton text" in v for v in out), 3, out)

    def test_after_naming_no_layer_is_refused(self):
        """An --after that matches no title is a usage error, not a silent append at the end."""
        # GIVEN two layers; WHEN --after names none; THEN exit 2
        self.assertEqual(self.add("Layer 9. x", "--after", "nope").returncode, 2)

    def test_page_without_layers_is_refused(self):
        """With no layer to follow, it refuses rather than guess where layers belong."""
        # GIVEN a page without layers; WHEN a layer is added; THEN exit 2
        self.page.write_text("<html><body><h2>Why</h2></body></html>")
        self.assertEqual(self.add("Layer 1. x").returncode, 2)


class NewPage(TmpCase):
    def setUp(self):
        super().setUp()
        self.p = make_project(self.tmp / "proj")
        self.tpl = self.tmp / "template.html"
        self.tpl.write_text("<html>TEMPLATE</html>")
        self.dir = self.tmp / "docs"
        self.dir.mkdir()

    def new(self, slug, *extra):
        return agent_cli(self.p, "page", "new", slug, "--dir", str(self.dir), "--template", str(self.tpl), *extra)

    def test_page_is_named_slug_and_date_and_copies_the_template(self):
        """A new page is <slug>-<date>.html holding the template, readable by others like any page."""
        # GIVEN a template; WHEN a page is started
        r = self.new("cache-layer", "--date", "2026-10-04")
        # THEN it exists with the template's content and a normal mode
        self.assertEqual(r.returncode, 0, r.stderr)
        made = self.dir / "cache-layer-2026-10-04.html"
        self.assertEqual(made.read_text(), "<html>TEMPLATE</html>")
        self.assertTrue(made.stat().st_mode & 0o044)

    def test_second_page_for_a_slug_is_refused(self):
        """One page per topic: a later `new` for the same slug on another day points at the existing page."""
        # GIVEN a page for the slug; WHEN another is started; THEN exit 2 telling the agent to update it
        self.new("cache-layer", "--date", "2026-10-04")
        again = self.new("cache-layer", "--date", "2026-10-05")
        self.assertEqual(again.returncode, 2)
        self.assertIn("update it instead", again.stderr)

    def test_slug_that_is_not_kebab_case_is_refused(self):
        """File names stay predictable; a slug with spaces or capitals is refused."""
        # GIVEN a slug with a space; WHEN started; THEN exit 2
        self.assertEqual(self.new("Cache Layer").returncode, 2)

    def test_template_comes_from_the_rules_file_relative_to_it(self):
        """A rules file names its template once, relative to itself, so callers need no --template."""
        # GIVEN a rules file naming template.html; WHEN a page starts without --template; THEN it is made
        rules_file = self.tmp / "page-rules.json"
        rules_file.write_text(json.dumps({"template": "template.html"}))
        r = agent_cli(self.p, "page", "new", "x", "--dir", str(self.dir), "--rules", str(rules_file))
        self.assertEqual(r.returncode, 0, r.stderr)


class RulesFile(TmpCase):
    def setUp(self):
        super().setUp()
        self.p = make_project(self.tmp / "proj")
        self.page = self.tmp / "p.html"
        self.page.write_text(doc(kicker="no draft here"))

    def check_with(self, rules_obj):
        f = self.tmp / "r.json"
        f.write_text(json.dumps(rules_obj))
        return agent_cli(self.p, "page", "check", str(self.page), "--rules", str(f))

    def test_bad_rules_files_are_refused_with_the_reason(self):
        """A typo or a wrong type must not turn a rule off silently, and a bad regex must not crash: exit 2 and why."""
        cases = {
            "unknown key kickr": {"kickr": {}},
            "unknown key layer.part": {"layer": {"part": ["h3"]}},
            "hidden_required must be a bool, not str": {"hidden_required": "yes"},
            "layer.parts must be a list, not str": {"layer": {"parts": "h3"}},
            "kicker.pattern: bad regex": {"kicker": {"pattern": "(unclosed"}},
            "layer.lead.p.pain: bad regex": {"layer": {"lead": {"p.pain": "[x"}}},
        }
        for want, obj in cases.items():
            with self.subTest(want):
                # GIVEN a rules file with one fault; WHEN used; THEN exit 2 naming it
                r = self.check_with(obj)
                self.assertEqual(r.returncode, 2, r.stdout)
                self.assertIn(want, r.stderr)

    def test_free_form_objects_and_null_rules_are_accepted(self):
        """layer.lead takes any selector as a key, and null turns a top-level rule off; both are valid files."""
        # GIVEN such a rules file; WHEN used; THEN it runs (exit 0 or 1, not 2)
        r = self.check_with({"layer": {"lead": {"p.any": "^x"}}, "template": None, "comment": "why"})
        self.assertIn(r.returncode, (0, 1), r.stderr)

    def test_seam_file_is_used_without_rules_flag(self):
        """With no --rules, the project's page-rules.json seam applies, so a project sets its rules once."""
        # GIVEN a seam that turns on the kicker rule
        seams = self.p / ".claude/agent-cli-hooks"
        os.makedirs(seams, exist_ok=True)
        (seams / "page-rules.json").write_text(json.dumps({"kicker": STRICT["kicker"]}))
        # WHEN checked without --rules; THEN the kicker rule fires
        r = agent_cli(self.p, "page", "check", str(self.page))
        self.assertEqual(r.returncode, 1)
        self.assertIn("kicker:", r.stdout)

    def test_several_pages_print_prefixed_lines(self):
        """With more than one page each line names its page, so a reader knows which file to fix."""
        # GIVEN a good and a bad page; WHEN checked together; THEN each line carries its page
        good, bad = self.tmp / "g.html", self.tmp / "b.html"
        good.write_text(doc())
        bad.write_text(doc(extra="<p>dash \u2014 here</p>"))
        r = agent_cli(self.p, "page", "check", str(good), str(bad))
        self.assertEqual(r.returncode, 1)
        self.assertIn(f"{good}: PASS", r.stdout)
        self.assertIn(f"{bad}: 'Why': em dash", r.stdout)

    def test_json_output_reports_pass(self):
        """--json gives one object a script can read."""
        # GIVEN a good page; WHEN checked with --json; THEN pass is true
        good = self.tmp / "g.html"
        good.write_text(doc())
        j = json.loads(agent_cli(self.p, "page", "check", str(good), "--json").stdout)
        self.assertEqual(j["pass"], True)


if __name__ == "__main__":
    unittest.main()
