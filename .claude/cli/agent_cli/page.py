"""`agent-cli page`: check a living HTML document page against format rules, and make the edits those rules
want by command (mark the latest update, add a layer skeleton, start a page from a template).

A page is plain HTML: an optional hero with a kicker line and a legend, sections, layers (one wrapper element
each, holding a heading, a pain point, a diagram and a done line), and one collapsed section for agents.
Every selector and pattern comes from the rules (see agent_cli/rules.py); the defaults below are the loose
floor, and the document skill's `.claude/skills/document/page-rules.json` tightens them. Stdlib only.
"""
import datetime
import html as htmllib
import math
import os
import re
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path

from . import core, rules as rulesmod
from .core import Exit
from .textrules import EM_DASH, forbidden

FILL = "FILL:"  # placeholder text in a new-layer skeleton; the default forbid_text fails it until replaced
DEFAULTS = {
    "hidden": "details.agents",        # the collapsed section for agents
    "hidden_name": "For agents",
    "hidden_required": False,
    "hidden_closed": False,            # true: the hidden section may not carry `open`
    "layer": {
        "wrap": "div.layer",
        "heading": r"Layer\s+\d",      # a title-tag heading starting like this must sit inside a wrapper
        "parts": ["h3", "p.pain", "figure.dia", "p.done"],  # parts[0] is the layer's title tag
        "ordered": False,
        "lead": {},                    # {part selector: regex the part's text must match at its start}
        "skeleton": None,              # new-layer HTML; {title} and {update_class} are filled in
    },
    "legend": {
        "wrap": "div.legend",
        "swatch": "sw",
        "max_swatches": 1,
        "line": None,                  # regex the legend text must match when it has a swatch
        "number": r"update (\d+)",
        "item": '<span class="{update_class}"><span class="{swatch}"></span> update {n}, {date}: {note}</span>',
    },
    "kicker": {"selector": None, "pattern": None, "draft": r"Draft (\d+)"},
    "update_class": "new",
    "require_update_mark": False,      # a legend swatch needs at least one marked element outside the legend
    "legacy_classes": None,            # regex: class names that may no longer appear (old update colours)
    "update_css": None,                # {"needs": regex, "css": text}: mark-update adds css when needs is absent
    "forbid_outside_hidden": ["div.upd", "div.ltodo"],
    "forbid_headings_outside_hidden": [],  # [{"pattern", "why"}] on h2/h3 text
    "forbid_text": [{"pattern": EM_DASH, "why": "em dash in visible text"},
                    {"pattern": re.escape(FILL), "why": "unfilled skeleton text"}],
    "unique_ids": False,
    "diagram_wiring": {                # off by default; three checks, see _Page.svg_done. Arrows must be <line>; transforms, <path> arrows, tspans unsupported
        "on": False,                   # inline <svg>: every box needs an arrow ending at its edge, its text must fit
        "box": "rect.bx",              # a box (rects shorter than min_height are pills and badges, not boxes)
        "min_height": 30.0,
        "label": "text.lb",            # box text in this selector is measured at label_px, other text at char_px
        "tolerance": 4.0,              # px between a <line> end and the box edge that still counts as touching
        "char_px": 6.2,
        "label_px": 7.2,
    },                                 # also: a legend swatch (legend.swatch) may not hold text, it makes the legend wrap
    "require_source": [],              # [{"pattern", "why"}] the raw HTML must match
    "template": None,                  # page new: default template path
}
CHECK = "agent-cli page check docs/cache-2026-01-01.html --rules .claude/skills/document/page-rules.json"
MARK = 'agent-cli page mark-update docs/cache-2026-01-01.html --note "layer 2 now covers eviction"'
LAYER = 'agent-cli page new-layer docs/cache-2026-01-01.html --title "Layer 3. Misses go to the database"'
NEW = "agent-cli page new cache --template .claude/skills/document/template.html --dir docs"


def register(sub):
    _, ps = core.group(sub, "page", "living HTML document pages: format check and the edits it wants",
                       [CHECK, MARK, LAYER])
    p = core.subcommand(ps, "check", "print each format violation (exit 1) or PASS (exit 0)", [CHECK])
    p.add_argument("pages", nargs="+", help="one or more .html pages")
    _rules_arg(p)
    p.set_defaults(func=check_cmd)
    p = core.subcommand(ps, "mark-update", "start a new update: strip the old update marks and legacy classes, "
                        "bump the draft number, write the one legend line; then mark your new text yourself",
                        [MARK])
    p.add_argument("page")
    p.add_argument("--note", required=True, help="what changed, one short clause (shown in the legend)")
    p.add_argument("--date", help="legend date text (default: today as e.g. '4 Oct')")
    _rules_arg(p)
    p.set_defaults(func=mark_update_cmd)
    p = core.subcommand(ps, "new-layer", "insert a layer skeleton (marked as the update) after the last layer or "
                        "after the last layer whose title contains --after; its FILL: text fails check until replaced",
                        [LAYER, LAYER + ' --after "Layer 2."'])
    p.add_argument("page")
    p.add_argument("--title", required=True, help="the layer's heading, stating its point")
    p.add_argument("--after", help="insert after the last layer whose title contains this (default: the last layer)")
    _rules_arg(p)
    p.set_defaults(func=new_layer_cmd)
    p = core.subcommand(ps, "new", "start <dir>/<slug>-<YYYY-MM-DD>.html from a template; refuses when a page for "
                        "the slug already exists (one page per topic)", [NEW])
    p.add_argument("slug", help="kebab-case topic, e.g. cache-layer")
    p.add_argument("--dir", required=True, help="folder for the page")
    p.add_argument("--template", help="template page (default: the rules file's `template`)")
    p.add_argument("--date", help="YYYY-MM-DD in the file name (default: today)")
    _rules_arg(p)
    p.set_defaults(func=new_cmd)


def _rules_arg(p):
    p.add_argument("--rules", help="rules JSON (default: the page-rules.json seam, else built-in defaults)")
    p.add_argument("--json", action="store_true", help="one JSON object")


def load_rules(path=None, example=CHECK):
    return rulesmod.load("page", DEFAULTS, path, example)


# ---------------------------------------------------------------- selectors

def parse_sel(sel):
    """'div.layer.new' -> ('div', {'layer', 'new'}); '.new' -> ('', {'new'})."""
    tag, *cls = sel.split(".")
    return tag.lower(), set(cls)


def matches(sel, tag, cls):
    t, need = parse_sel(sel)
    return (not t or t == tag) and need <= cls


def element_re(sel):
    """A regex for the first (non-nested) element matching a simple selector; group 1 is its inner HTML."""
    tag, cls = parse_sel(sel)
    tag = tag or r"[a-zA-Z][a-zA-Z0-9]*"
    look = "".join(rf'(?=[^>]*\bclass="[^"]*(?<![\w-]){re.escape(c)}(?![\w-])[^"]*")' for c in sorted(cls))
    return re.compile(rf"<({tag})\b{look}[^>]*>(.*?)</\1\s*>", re.S | re.I)


def text_of(fragment):
    return " ".join(htmllib.unescape(re.sub(r"<[^>]+>", "", fragment)).split())


# ---------------------------------------------------------------- check

class _Page(HTMLParser):
    """One pass over the page: collects violations and where each layer starts and ends."""

    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}

    def __init__(self, src, rules):
        super().__init__(convert_charrefs=True)
        self.src, self.r, self.L = src, rules, rules["layer"]
        self.lines = [0] + [m.end() for m in re.finditer("\n", src)]
        self.out, self.layers = [], []
        self.wrap_tag = parse_sel(self.L["wrap"])[0]
        self.title_tag = parse_sel(self.L["parts"][0])[0]
        self.hidden_tag = parse_sel(rules["hidden"])[0] if rules["hidden"] else None
        self.hidden = 0           # depth inside <style>/<script>
        self.layer_depth = 0      # wrap-tag depth inside the current layer (0 = not in a layer)
        self.agents = 0           # hidden-tag depth inside the hidden section
        self.agents_found = self.agents_open = False
        self.legend_depth = 0
        self.legend_found, self.legend_text, self.swatches = False, "", 0
        self.marks_outside_legend = 0
        self.heading, self.in_heading = "top of page", None
        self.second_title = False
        self.caps = []            # open text captures: [tag, depth, purpose, buffer]
        self.kicker = None
        self.ids, self.legacy = Counter(), set()
        self.svg = None           # the inline <svg> being read when diagram_wiring is on

    def abs_pos(self):
        line, col = self.getpos()
        return self.lines[line - 1] + col

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = set((a.get("class") or "").split())
        if a.get("id"):
            self.ids[a["id"]] += 1
        if self.r["legacy_classes"]:
            self.legacy |= {c for c in cls if re.fullmatch(self.r["legacy_classes"], c)}
        for cap in self.caps:
            if cap[0] == tag:
                cap[1] += 1
        if tag in ("style", "script"):
            self.hidden += 1
        if self.r["diagram_wiring"]["on"]:
            self.svg_start(tag, a, cls)
        if self.hidden_tag and tag == self.hidden_tag:
            if self.agents:
                self.agents += 1
            elif matches(self.r["hidden"], tag, cls):
                self.agents, self.agents_found = 1, True
                self.agents_open = self.agents_open or "open" in a
        if tag == self.wrap_tag:
            if matches(self.L["wrap"], tag, cls):  # layers never nest: a new one closes any open one
                if self.layers and self.layer_depth:
                    self.layers[-1]["end"] = self.abs_pos()
                self.layer_depth = 1
                self.layers.append({"name": f"layer {len(self.layers) + 1}", "seen": [], "lead": {},
                                    "start": self.abs_pos(), "end": None})
            elif self.layer_depth:
                self.layer_depth += 1
        if not self.agents:
            for sel in self.r["forbid_outside_hidden"]:
                if matches(sel, tag, cls):
                    self.out.append(f"'{self.heading}': {sel} outside {self.r['hidden_name']}, move it there")
        lg = self.r["legend"]
        lg_tag = parse_sel(lg["wrap"])[0]
        if self.legend_depth and tag == lg_tag:
            self.legend_depth += 1
        elif not self.legend_depth and matches(lg["wrap"], tag, cls):
            self.legend_depth, self.legend_found = 1, True
            self.caps.append([tag, 1, "legend", []])
        if self.legend_depth and lg["swatch"] in cls:
            self.swatches += 1
            if self.r["diagram_wiring"]["on"]:
                self.caps.append([tag, 1, "swatch", []])
        elif not self.legend_depth and self.r["update_class"] in cls:
            self.marks_outside_legend += 1
        if tag in ("h1", "h2", "h3", "h4"):
            self.in_heading, self.heading = tag, ""
        k = self.r["kicker"]
        if k["selector"] and self.kicker is None and matches(k["selector"], tag, cls):
            self.kicker = ""
            self.caps.append([tag, 1, "kicker", []])
        if self.layer_depth:
            lay = self.layers[-1]
            for sel in self.L["parts"]:
                if matches(sel, tag, cls):
                    if sel == self.L["parts"][0]:
                        self.second_title = sel in lay["seen"]
                    if sel not in lay["seen"]:
                        lay["seen"].append(sel)
                        if sel in self.L["lead"]:
                            self.caps.append([tag, 1, ("lead", lay, sel), []])
                    break

    def handle_endtag(self, tag):
        for cap in list(self.caps):
            if cap[0] == tag:
                cap[1] -= 1
                if cap[1] == 0:
                    self.caps.remove(cap)
                    self.finish(cap[2], " ".join("".join(cap[3]).split()))
        if tag in ("style", "script") and self.hidden:
            self.hidden -= 1
        if self.svg and tag in ("svg", "defs"):
            self.svg[tag] -= 1
            if tag == "svg" and not self.svg["svg"]:
                self.svg_done()
        if self.hidden_tag and tag == self.hidden_tag and self.agents:
            self.agents -= 1
        if tag == self.wrap_tag and self.layer_depth:
            self.layer_depth -= 1
            if not self.layer_depth:
                self.layers[-1]["end"] = self.end_offset(tag)
        lg_tag = parse_sel(self.r["legend"]["wrap"])[0]
        if tag == lg_tag and self.legend_depth:
            self.legend_depth -= 1
        if tag == self.in_heading:
            self.in_heading = None
            self.heading = " ".join(self.heading.split())
            self.heading_done(tag)

    def end_offset(self, tag):
        """Offset just past the end tag the parser is on."""
        start = self.abs_pos()
        m = re.compile(rf"</{tag}\s*>", re.I).match(self.src, start)
        return m.end() if m else start

    def heading_done(self, tag):
        if tag == self.title_tag and self.layer_depth and self.second_title:
            self.out.append(f"unclosed {self.L['wrap']} before '{self.heading}'")
        elif tag == self.title_tag and self.layer_depth:
            self.layers[-1]["name"] = self.heading
        elif tag == self.title_tag and not self.agents and re.match(self.L["heading"], self.heading):
            self.out.append(f"'{self.heading}': layer not wrapped in {self.L['wrap']}")
        if tag in ("h2", "h3") and not self.agents:
            for why, _ in forbidden(self.heading, self.r["forbid_headings_outside_hidden"]):
                self.out.append(f"'{self.heading}': {why}")

    def finish(self, purpose, text):
        if purpose == "legend":
            self.legend_text = text
        elif purpose == "kicker":
            self.kicker = text
        elif purpose == "swatch":
            if text:
                self.out.append(f"'{self.heading}': legend swatch holds text '{text[:40]}'; the swatch is an empty "
                                "square, put the words beside it (a swatch with text makes the legend wrap)")
        elif purpose[0] == "text":
            purpose[1]["text"] = text
        else:
            _, lay, sel = purpose
            lay["lead"][sel] = text

    def svg_start(self, tag, a, cls):
        """Collect the boxes, arrows and text of one inline <svg> (arrow = <line>; <defs> are skipped)."""
        dw = self.r["diagram_wiring"]
        if tag == "svg":
            if self.svg:
                self.svg["svg"] += 1
            else:
                self.svg = {"svg": 1, "defs": 0, "boxes": [], "ends": [], "texts": []}
            return
        s = self.svg
        if not s:
            return
        if tag == "defs":
            s["defs"] += 1
        if s["defs"]:
            return
        try:
            n = lambda k: float(a[k])  # noqa: E731
            if tag == "rect" and matches(dw["box"], tag, cls) and n("height") >= dw["min_height"]:
                s["boxes"].append((n("x"), n("y"), n("width"), n("height")))
            elif tag == "line":
                s["ends"] += [(n("x1"), n("y1")), (n("x2"), n("y2"))]
            elif tag == "text":
                rec = {"x": n("x"), "y": n("y"), "anchor": a.get("text-anchor", "start"),
                       "label": matches(dw["label"], tag, cls), "text": ""}
                s["texts"].append(rec)
                self.caps.append([tag, 1, ("text", rec), []])
        except (KeyError, ValueError):
            pass  # a shape without plain numeric coordinates cannot be checked; leave it alone

    def svg_done(self):
        """Report, for the finished <svg>, each box no arrow touches and each text wider than its box."""
        s, dw, self.svg = self.svg, self.r["diagram_wiring"], None
        where = self.layers[-1]["name"] if self.layer_depth else self.heading
        boxes = sorted(s["boxes"], key=lambda b: b[2] * b[3])  # smallest first: a text belongs to the tightest box
        names = {b: [] for b in boxes}
        for t in s["texts"]:
            home = next((b for b in boxes if b[0] <= t["x"] <= b[0] + b[2] and b[1] <= t["y"] <= b[1] + b[3]), None)
            if home:
                names[home].append(t)
        for b in boxes:
            x, y, w, _ = b
            name = (names[b][0]["text"] if names[b] else "") or f"at x={x:g}, y={y:g}"
            if not any(_gap(px, py, b) <= dw["tolerance"] for px, py in s["ends"]):
                self.out.append(f"'{where}': loose box '{name}': no <line> arrow ends within {dw['tolerance']:g} px of it; "
                                "connect it to the box it depends on, or delete it")
            for t in names[b]:
                wide = len(t["text"]) * (dw["label_px"] if t["label"] else dw["char_px"])
                left = t["x"] - {"middle": wide / 2, "end": wide}.get(t["anchor"], 0)
                if left < x or left + wide > x + w:
                    self.out.append(f"'{where}': text '{t['text']}' is about {wide:g} px wide, the box '{name}' "
                                    f"{w:g}; shorten it or split the line")

    def handle_data(self, data):
        if self.hidden:
            return
        if self.in_heading:
            self.heading += data
        for cap in self.caps:
            cap[3].append(data)
        for why, _ in forbidden(data, self.r["forbid_text"]):
            snippet = " ".join(data.split())[:60]
            self.out.append(f"'{self.heading}': {why}: {snippet}")


def _gap(px, py, box):
    """Distance from a point to a box's outline (to the box when outside it)."""
    x, y, w, h = box
    dx, dy = max(x - px, 0, px - x - w), max(y - py, 0, py - y - h)
    return math.hypot(dx, dy) if dx or dy else min(px - x, x + w - px, py - y, y + h - py)


def parse(src, rules):
    p = _Page(src, rules)
    p.feed(src)
    p.close()
    return p


def check_page(src, rules=None):
    """Return the list of violations for one page's HTML."""
    r = rules or load_rules()[0]
    p = parse(src, r)
    out, L, lg = p.out, r["layer"], r["legend"]
    for layer in p.layers:
        for sel in L["parts"]:
            if sel not in layer["seen"]:
                out.append(f"'{layer['name']}': layer lacks {sel}")
        if L["ordered"]:
            want = [s for s in L["parts"] if s in layer["seen"]]
            if layer["seen"] != want:
                out.append(f"'{layer['name']}': parts out of order ({', '.join(layer['seen'])}); "
                           f"want {', '.join(L['parts'])}")
        for sel, lead in L["lead"].items():
            if sel in layer["lead"] and not re.match(lead, layer["lead"][sel]):
                out.append(f"'{layer['name']}': {sel} must start with /{lead}/")
    if p.swatches > lg["max_swatches"]:
        out.append(f"legend: {p.swatches} update swatches, keep only the latest one")
    if p.swatches and lg["line"] and not re.search(lg["line"], p.legend_text):
        out.append(f"legend: the update line '{p.legend_text[:80]}' does not match '{lg['line']}'; "
                   "write it with `agent-cli page mark-update`")
    if r["require_update_mark"] and p.swatches and not p.marks_outside_legend:
        out.append(f"legend names an update but no element outside the legend has class "
                   f"'{r['update_class']}'; mark this update's new text")
    k = r["kicker"]
    if k["selector"] and k["pattern"]:
        if p.kicker is None:
            out.append(f"kicker: no {k['selector']} on the page")
        elif not re.search(k["pattern"], p.kicker):
            out.append(f"kicker: '{p.kicker[:80]}' does not match '{k['pattern']}'")
    if r["hidden"] and r["hidden_required"] and not p.agents_found:
        out.append(f"no {r['hidden']} section ({r['hidden_name']})")
    if r["hidden_closed"] and p.agents_open:
        out.append(f"{r['hidden_name']} ({r['hidden']}) is open; it must start closed")
    for c in sorted(p.legacy):
        out.append(f"class '{c}' is a legacy update class; run `agent-cli page mark-update`")
    if r["unique_ids"]:
        for i, n in sorted(p.ids.items()):
            if n > 1:
                out.append(f"id '{i}' is used {n} times; ids (arrow markers too) must be unique on the page")
    for need in r["require_source"]:
        if not re.search(need["pattern"], src):
            out.append(need.get("why") or f"page lacks {need['pattern']!r}")
    return out


def check_cmd(a):
    r, _ = load_rules(a.rules)
    bad = 0
    results = []
    for path in a.pages:
        try:
            out = check_page(Path(path).read_text(encoding="utf-8"), r)
        except (OSError, UnicodeDecodeError) as e:
            core.die(Exit.REFUSED, f"cannot read {path}: {e}", CHECK)
        bad += bool(out)
        results.append({"page": path, "violations": out})
        if not a.json:
            prefix = f"{path}: " if len(a.pages) > 1 else ""
            print("\n".join(prefix + v for v in out) if out else prefix + "PASS")
    if a.json:
        core.emit({"pass": not bad, "pages": results}, True)
    return Exit.FAIL if bad else Exit.OK


# ---------------------------------------------------------------- edits

def read(path, example):
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        core.die(Exit.REFUSED, f"cannot read {path}: {e}", example)


def strip_classes(src, drop):
    """Remove class names for which drop(name) is true from every class="..." attribute outside scripts, styles
    and comments; an emptied attribute is removed, and a span left with no attributes is unwrapped.
    Returns (new src, how many names were removed)."""
    n = 0

    def fix(m):
        nonlocal n
        names = m.group(2).split()
        keep = [c for c in names if not drop(c)]
        n += len(names) - len(keep)
        if len(keep) == len(names):
            return m.group(0)
        if not keep and re.fullmatch(r"<span", m.group(1), re.I) and m.group(3).strip() == ">":
            return UNWRAP + ">"  # a span that only carried the mark: unwrapped below
        return m.group(1) + (f' class="{" ".join(keep)}"' if keep else "") + m.group(3)
    parts = re.split(r"(<!--.*?-->|<script\b.*?</script\s*>|<style\b.*?</style\s*>)", src, flags=re.S | re.I)
    for i in range(0, len(parts), 2):
        parts[i] = re.sub(r'(<[a-zA-Z][a-zA-Z0-9]*\b[^>]*?)\s+class="([^"]*)"([^>]*>)', fix, parts[i])
    return unwrap("".join(parts)), n


UNWRAP = "<span data-agent-cli-unwrap"


def unwrap(src):
    """Drop each `<span data-agent-cli-unwrap>` and its matching </span>, keeping the text between."""
    out, stack, pos = [], [], 0
    for m in re.finditer(r"<span\b[^>]*>|</span\s*>", src, re.I):
        if m.group().startswith("</"):
            if stack and stack.pop():
                out.append(src[pos:m.start()])
                pos = m.end()
        else:
            drop = m.group().startswith(UNWRAP)
            stack.append(drop)
            if drop:
                out.append(src[pos:m.start()])
                pos = m.end()
    out.append(src[pos:])
    return "".join(out)


def today_label(d=None):
    d = d or datetime.date.today()
    return f"{d.day} {d:%b}"


def mark_update(src, note, date_label, r):
    """The page with the last update's marks stripped, the draft bumped and one new legend line.
    Returns (new src, info dict)."""
    lg, k, uc = r["legend"], r["kicker"], r["update_class"]
    legacy = r["legacy_classes"]
    m = element_re(lg["wrap"]).search(src)
    if not m:
        raise ValueError(f"no {lg['wrap']} legend on the page to write the update line into")
    kept, items = legend_items(m.group(2), lg["swatch"])
    nums = [int(n) for it in items for n in re.findall(lg["number"], text_of(it))]
    info = {"marks_removed": 0, "draft": None, "update": max(nums, default=0) + 1}
    src = src[:m.start(2)] + kept.rstrip() + "\n    " + src[m.end(2):]
    src, info["marks_removed"] = strip_classes(src, lambda c: c == uc or bool(legacy and re.fullmatch(legacy, c)))
    if k["selector"]:
        m = element_re(k["selector"]).search(src)
        d = re.search(k["draft"], m.group(2)) if m else None
        if d:
            info["draft"] = int(d.group(1)) + 1
            inner = m.group(2)[:d.start(1)] + str(info["draft"]) + m.group(2)[d.end(1):]
            src = src[:m.start(2)] + inner + src[m.end(2):]
    m = element_re(lg["wrap"]).search(src)
    line = lg["item"].format(update_class=uc, swatch=lg["swatch"], n=info["update"], date=htmllib.escape(date_label),
                             note=htmllib.escape(note, quote=False))
    src = src[:m.start(2)] + m.group(2).rstrip() + f"\n      {line}\n    " + src[m.end(2):]
    css = r["update_css"]
    if css and not re.search(css["needs"], src):
        i = src.find("</style>")
        if i < 0:
            raise ValueError("no </style> to add the update css to")
        src = src[:i] + css["css"].rstrip() + "\n" + src[i:]
        info["css_added"] = True
    left = parse(src, r).swatches
    if left > lg["max_swatches"]:
        raise ValueError(f"the legend still has {left} swatches after the edit; its old lines have a shape "
                         "mark-update does not know, remove them by hand")
    return src, info


def legend_items(inner, swatch):
    """(inner without its update lines, [removed lines]): an update line is a top-level element of the legend
    that holds an element of class `swatch` or has that class itself; tags are matched with nesting."""
    has = re.compile(rf'class="[^"]*(?<![\w-]){re.escape(swatch)}(?![\w-])')
    kept, items, depth, start, pos = [], [], 0, 0, 0
    for t in re.finditer(r"<(/?)([a-zA-Z][a-zA-Z0-9]*)\b[^>]*?(/?)>", inner):
        if t.group(2).lower() in _Page.VOID or t.group(3):
            if depth == 0 and has.search(t.group()):
                kept.append(inner[pos:t.start()])
                items.append(t.group())
                pos = t.end()
            continue
        if not t.group(1):
            if depth == 0:
                start = t.start()
            depth += 1
        elif depth:
            depth -= 1
            if depth == 0 and has.search(inner[start:t.end()]):
                kept.append(inner[pos:start])
                items.append(inner[start:t.end()])
                pos = t.end()
    kept.append(inner[pos:])
    return "".join(kept), items


def mark_update_cmd(a):
    r, _ = load_rules(a.rules, MARK)
    if not a.note.strip():
        core.die(Exit.REFUSED, "--note is empty", MARK)
    hits = forbidden(a.note, r["forbid_text"])
    if hits:
        core.die(Exit.REFUSED, f"--note has {hits[0][0]}", MARK)
    src = read(a.page, MARK)
    try:
        new, info = mark_update(src, a.note.strip(), a.date or today_label(), r)
    except ValueError as e:
        core.die(Exit.REFUSED, str(e), MARK)
    core.write_atomic(a.page, new)
    info["page"] = a.page
    info["next"] = (f"wrap this update's new or changed text in class \"{r['update_class']}\", "
                    f"then: agent-cli page check {a.page}")
    core.emit(info, a.json)
    return Exit.OK


def skeleton(title, r):
    L, uc = r["layer"], r["update_class"]
    if L["skeleton"]:
        return L["skeleton"].format(title=htmllib.escape(title, quote=False), update_class=uc)
    wtag, wcls = parse_sel(L["wrap"])
    parts = []
    for i, sel in enumerate(L["parts"]):
        tag, cls = parse_sel(sel)
        attr = f' class="{" ".join(sorted(cls))}"' if cls else ""
        body = htmllib.escape(title, quote=False) if i == 0 else f"{FILL} {sel}"
        parts.append(f"  <{tag or 'div'}{attr}>{body}</{tag or 'div'}>")
    cls = " ".join(sorted(wcls | {uc}))
    return f'<{wtag or "div"} class="{cls}">\n' + "\n".join(parts) + f"\n</{wtag or 'div'}>"


def new_layer(src, title, after, r):
    """The page with a layer skeleton inserted after the chosen layer. Raises ValueError when there is none."""
    p = parse(src, r)
    layers = [x for x in p.layers if x["end"] is not None]
    if after:
        layers = [x for x in layers if after in x["name"]]
        if not layers:
            raise ValueError(f"no layer title contains {after!r}")
    if not layers:
        raise ValueError(f"no {r['layer']['wrap']} on the page to insert after; give --after or add one by hand")
    end = layers[-1]["end"]
    return src[:end] + "\n" + skeleton(title, r) + "\n" + src[end:]


def new_layer_cmd(a):
    r, _ = load_rules(a.rules, LAYER)
    src = read(a.page, LAYER)
    try:
        new = new_layer(src, a.title.strip(), a.after, r)
    except ValueError as e:
        core.die(Exit.REFUSED, str(e), LAYER)
    core.write_atomic(a.page, new)
    core.out(a, {"page": a.page, "layer": a.title, "status": "inserted"},
             f"inserted: {a.title}\nnext: replace every {FILL} line, then agent-cli page check {a.page}")
    return Exit.OK


def new_cmd(a):
    r, src_rules = load_rules(a.rules, NEW)
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", a.slug):
        core.die(Exit.REFUSED, f"slug {a.slug!r} is not kebab-case", NEW)
    template = a.template or (rulesmod.resolve(r["template"], src_rules.parent if src_rules else None)
                              if r["template"] else None)
    if not template or not os.path.isfile(template):
        core.die(Exit.REFUSED, f"no template file: {template or '(none given)'}", NEW)
    try:
        date = datetime.date.fromisoformat(a.date) if a.date else datetime.date.today()
    except ValueError:
        core.die(Exit.REFUSED, f"--date {a.date!r} is not YYYY-MM-DD", NEW)
    folder = Path(a.dir)
    if not folder.is_dir():
        core.die(Exit.REFUSED, f"no such folder: {folder}", NEW)
    pat = re.compile(rf"{re.escape(a.slug)}-\d{{4}}-\d\d-\d\d\.html")
    existing = sorted(f for f in os.listdir(folder) if pat.fullmatch(f))
    if existing:
        core.die(Exit.REFUSED, f"a page for {a.slug!r} exists: {folder / existing[0]}; update it instead", NEW)
    dest = folder / f"{a.slug}-{date.isoformat()}.html"
    core.write_atomic(dest, read(template, NEW))
    core.out(a, {"page": str(dest), "status": "created"}, f"created: {dest}")
    return Exit.OK

