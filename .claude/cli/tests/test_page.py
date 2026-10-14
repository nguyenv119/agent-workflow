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
    "layer": {"ordered": True, "lead": {"p.pain": "Original pain point:", "p.done": "Done:"}},
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
        cases = {
            "parts out of order": doc([layer(body=FIG + PAIN + "<p>x</p>" + DONE)]),
            "must start with 'Original pain point:'": doc([layer(body='<p class="pain">pain</p><p>x</p>' + FIG + DONE)]),
            "must start with 'Done:'": doc([layer(body=PAIN + FIG + '<p class="done">built</p>')]),
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
                out = self.check(html)
                self.assertTrue(any(want in v for v in out), out)

    def test_hidden_section_may_hold_forbidden_headings_and_upd_boxes(self):
        """Inside the agents section, Open questions and old update boxes are allowed: that is where they go."""
        # GIVEN those inside details.agents
        agents = ('<details class="agents"><summary>For agents</summary><h2>Open questions</h2>'
                  '<div class="upd">old</div></details>')
        # WHEN checked; THEN no violation
        self.assertEqual(self.check(doc(agents=agents)), [])

    def test_unfilled_skeleton_text_fails(self):
        """A FILL: placeholder left in visible text fails, so a new-layer skeleton cannot ship half done."""
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

    def test_old_marks_go_draft_and_update_count_up_and_page_then_needs_new_marks(self):
        """mark-update strips every old mark (unwrapping mark-only spans), drops old legend lines and legacy
        classes, bumps the draft, writes update N+1, adds the update css; the page then fails until new text is marked."""
        # GIVEN a page with marked text, a legacy class and two legend lines
        legend = ('<span class="u1"><span class="sw"></span> update 1, 2 Oct: a</span>'
                  '<span class="new"><span class="sw"></span> update 2, 4 Oct: b</span>')
        self.page.write_text(doc(legend=legend, extra='<p>keep <span class="new">this</span></p>'
                                                      '<p class="new u2 big">marked</p>'))
        # WHEN mark-update runs
        r = self.run_cli("mark-update", str(self.page), "--note", "layer 2 covers <eviction>", "--date", "14 Oct", "--json")
        # THEN it reports the new numbers and the page holds one line, no old marks, the css
        self.assertEqual(r.returncode, 0, r.stderr)
        info = json.loads(r.stdout)
        self.assertEqual((info["draft"], info["update"], info.get("css_added")), (4, 3, True))
        html = self.page.read_text()
        self.assertIn("<p>keep this</p>", html)
        self.assertIn('<p class="big">marked</p>', html)
        self.assertIn("update 3, 14 Oct: layer 2 covers &lt;eviction&gt;", html)
        self.assertNotIn("update 2", html)
        self.assertIn("Draft 4 ", html)
        self.assertIn(".new { color: blue; }", html)
        out = self.run_cli("check", str(self.page))
        self.assertEqual(out.returncode, 1)
        self.assertIn("no element outside the legend", out.stdout)

    def test_note_with_a_forbidden_string_is_refused_and_page_untouched(self):
        """A note carrying an em dash would plant a violation in the legend; it is refused before any write."""
        self.page.write_text(doc())
        before = self.page.read_text()
        r = self.run_cli("mark-update", str(self.page), "--note", "a — b")
        self.assertEqual(r.returncode, 2)
        self.assertEqual(self.page.read_text(), before)

    def test_page_without_legend_is_refused(self):
        self.page.write_text("<html><body><p>no legend</p></body></html>")
        r = self.run_cli("mark-update", str(self.page), "--note", "x")
        self.assertEqual(r.returncode, 2)
        self.assertIn("legend", r.stderr)


class NewLayer(TmpCase):
    def setUp(self):
        super().setUp()
        self.p = make_project(self.tmp / "proj")
        self.page = self.tmp / "p.html"

    def test_skeleton_goes_after_the_named_or_last_layer_and_fails_check_until_filled(self):
        """new-layer inserts a full skeleton (all parts, marked new) right after the chosen layer; the FILL text fails."""
        # GIVEN two layers
        self.page.write_text(doc([layer("Layer 1. One"), layer("Layer 2. Two")]))
        # WHEN a layer goes after layer 1, and one after the last
        r1 = agent_cli(self.p, "page", "new-layer", str(self.page), "--title", "Layer 1a. Zoom", "--after", "Layer 1.")
        r2 = agent_cli(self.p, "page", "new-layer", str(self.page), "--title", "Layer 3. Three")
        # THEN the order is 1, 1a, 2, 3 and each new layer has every part
        self.assertEqual((r1.returncode, r2.returncode), (0, 0), r1.stderr + r2.stderr)
        html = self.page.read_text()
        order = [html.index(t) for t in ("Layer 1. One", "Layer 1a. Zoom", "Layer 2. Two", "Layer 3. Three")]
        self.assertEqual(order, sorted(order))
        out = page.check_page(html, page.load_rules()[0])
        self.assertEqual(sum("unfilled skeleton text" in v for v in out), 6, out)
        self.assertFalse(any("lacks" in v for v in out), out)
        self.assertEqual(html.count('class="layer new"'), 2)

    def test_no_layer_to_follow_is_refused(self):
        self.page.write_text("<html><body><h2>Why</h2></body></html>")
        r = agent_cli(self.p, "page", "new-layer", str(self.page), "--title", "Layer 1. x")
        self.assertEqual(r.returncode, 2)
        r = agent_cli(self.p, "page", "new-layer", str(self.page), "--title", "Layer 1. x", "--after", "nope")
        self.assertEqual(r.returncode, 2)


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

    def test_page_is_named_slug_date_and_a_second_page_for_the_slug_is_refused(self):
        """One page per topic: the first `new` copies the template, a later one on another day is refused."""
        r = self.new("cache-layer", "--date", "2026-10-04")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.dir / "cache-layer-2026-10-04.html").read_text(), "<html>TEMPLATE</html>")
        again = self.new("cache-layer", "--date", "2026-10-05")
        self.assertEqual(again.returncode, 2)
        self.assertIn("update it instead", again.stderr)
        self.assertEqual(self.new("Cache Layer").returncode, 2)

    def test_template_comes_from_the_rules_file_relative_to_it(self):
        rules = self.tmp / "page-rules.json"
        rules.write_text(json.dumps({"template": "template.html"}))
        r = agent_cli(self.p, "page", "new", "x", "--dir", str(self.dir), "--rules", str(rules))
        self.assertEqual(r.returncode, 0, r.stderr)


class RulesFile(TmpCase):
    def test_unknown_key_is_refused_and_seam_file_is_used(self):
        """A typo in a rules file must not silently turn a rule off; the seam is read when --rules is absent."""
        p = make_project(self.tmp / "proj")
        f = self.tmp / "p.html"
        f.write_text(doc(kicker="no draft here"))
        bad = self.tmp / "bad.json"
        bad.write_text(json.dumps({"kickr": {}}))
        r = agent_cli(p, "page", "check", str(f), "--rules", str(bad))
        self.assertEqual(r.returncode, 2)
        self.assertIn("unknown keys: kickr", r.stderr)
        seams = p / ".claude/agent-cli-hooks"
        os.makedirs(seams, exist_ok=True)
        (seams / "page-rules.json").write_text(json.dumps({"kicker": STRICT["kicker"]}))
        r = agent_cli(p, "page", "check", str(f))
        self.assertEqual(r.returncode, 1)
        self.assertIn("kicker:", r.stdout)

    def test_several_pages_print_prefixed_lines_and_json(self):
        p = make_project(self.tmp / "proj")
        good, bad = self.tmp / "g.html", self.tmp / "b.html"
        good.write_text(doc())
        bad.write_text(doc(extra="<p>dash — here</p>"))
        r = agent_cli(p, "page", "check", str(good), str(bad))
        self.assertEqual(r.returncode, 1)
        self.assertIn(f"{good}: PASS", r.stdout)
        self.assertIn(f"{bad}: 'Why': em dash", r.stdout)
        j = json.loads(agent_cli(p, "page", "check", str(good), "--json").stdout)
        self.assertEqual(j["pass"], True)


if __name__ == "__main__":
    unittest.main()
