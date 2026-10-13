#!/usr/bin/env python3
"""Check a document page or a folder of desk cards against the format rules a script can check.

    check_page.py <page.html>      every div.layer has h3, p.pain, figure.dia, p.done; one legend
                                   swatch; no em dash in visible text
    check_page.py cards <dir>      needs-you cards fill every decision field; prose has no tracker
                                   ids or PR numbers; no token-shaped strings; no em dashes

Prints one line per violation and exits 1, or prints PASS and exits 0. Exit 2 on bad usage.
Stdlib only.
"""
import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

EM_DASH = "—"
CARD_FIELDS = ("title", "background", "why", "choice", "recommendation", "detail")
PROSE_FIELDS = ("title", "background", "why", "choice", "recommendation")  # detail is a path
LAYER_PARTS = (("h3", "h3"), ("pain", "p.pain"), ("figure", "figure.dia"), ("done", "p.done"))

# A tracker id: prefix-<3-5 chars holding a letter and a digit>[.N], or a bare 4-5 char id with .N.
# Requiring both a letter and a digit keeps "follow-up", "top-100" and "covid-19" out.
BEAD = re.compile(r"\b[a-z][a-z0-9]*(?:-[a-z0-9]+)*-(?=[a-z0-9]{3,5}\b)(?=[a-z0-9]*\d)(?=[a-z0-9]*[a-z])"
                  r"[a-z0-9]+(?:\.\d+)?\b"
                  r"|\b(?=[a-z0-9]{4,5}\.\d)(?=[a-z0-9]*\d)(?=[a-z0-9]*[a-z])[a-z0-9]+\.\d+\b")
PR = re.compile(r"(?<![&\w])#\d+\b")
TOKENS = (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), re.compile(r"\bghp_[A-Za-z0-9]{20,}"),
          re.compile(r"\bAKIA[A-Z0-9]{16}\b"), re.compile(r"\b[0-9a-fA-F]{32,}\b"),
          # base64-ish: 32+ chars mixing upper, lower and digit (a slug or path has no such run)
          re.compile(r"(?<![A-Za-z0-9+_=-])(?=[A-Za-z0-9+_=-]*[A-Z])(?=[A-Za-z0-9+_=-]*[a-z])"
                     r"(?=[A-Za-z0-9+_=-]*\d)[A-Za-z0-9+_=-]{32,}"))


class _Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.layers = [], []
        self.hidden = 0           # depth inside <style>/<script>
        self.layer_div = 0        # div depth inside the current layer (0 = not in a layer)
        self.legend_div = 0
        self.swatches = 0
        self.heading, self.in_heading = "top of page", None

    def handle_starttag(self, tag, attrs):
        cls = (dict(attrs).get("class") or "").split()
        if tag in ("style", "script"):
            self.hidden += 1
        if tag == "div":
            if self.layer_div:
                self.layer_div += 1
            elif "layer" in cls:
                self.layer_div = 1
                self.layers.append({"name": f"layer {len(self.layers) + 1}", "has": set()})
            if self.legend_div:
                self.legend_div += 1
            elif "legend" in cls:
                self.legend_div = 1
        if self.legend_div and "sw" in cls:
            self.swatches += 1
        if tag in ("h1", "h2", "h3", "h4"):
            self.in_heading, self.heading = tag, ""
        if self.layer_div:
            has = self.layers[-1]["has"]
            if tag == "h3":
                has.add("h3")
            elif tag == "p" and "pain" in cls:
                has.add("pain")
            elif tag == "p" and "done" in cls:
                has.add("done")
            elif tag == "figure" and "dia" in cls:
                has.add("figure")

    def handle_endtag(self, tag):
        if tag in ("style", "script") and self.hidden:
            self.hidden -= 1
        if tag == "div":
            self.layer_div = max(self.layer_div - 1, 0)
            self.legend_div = max(self.legend_div - 1, 0)
        if tag == self.in_heading:
            self.in_heading = None
            self.heading = " ".join(self.heading.split())
            if tag == "h3" and self.layer_div:
                self.layers[-1]["name"] = self.heading

    def handle_data(self, data):
        if self.hidden:
            return
        if self.in_heading:
            self.heading += data
        if EM_DASH in data:
            snippet = " ".join(data.split())[:60]
            self.out.append(f"'{self.heading}': em dash in visible text: {snippet}")


def check_page(html):
    """Return the list of violations for one page's HTML."""
    p = _Page()
    p.feed(html)
    p.close()
    out = p.out
    for layer in p.layers:
        for key, label in LAYER_PARTS:
            if key not in layer["has"]:
                out.append(f"'{layer['name']}': layer lacks {label}")
    if p.swatches > 1:
        out.append(f"legend: {p.swatches} update swatches, keep only the latest one")
    return out


def check_cards(folder):
    """Return the list of violations for every *.json desk card in folder."""
    out = []
    for f in sorted(Path(folder).glob("*.json")):
        try:
            card = json.loads(f.read_text())
        except (OSError, ValueError) as e:
            out.append(f"{f.name}: not readable JSON ({e})")
            continue
        if not isinstance(card, dict):
            out.append(f"{f.name}: card is not a JSON object")
            continue
        if card.get("status") == "needs-you":
            for field in CARD_FIELDS:
                v = card.get(field)
                if not isinstance(v, str) or not v.strip():
                    out.append(f"{f.name}: {field}: needs-you card is missing this field")
        for field, v in card.items():
            if not isinstance(v, str):
                continue
            if EM_DASH in v:
                out.append(f"{f.name}: {field}: em dash")
            if field in PROSE_FIELDS:
                if m := BEAD.search(v):
                    out.append(f"{f.name}: {field}: bead id '{m.group()}' in prose, name the work in words")
                if m := PR.search(v):
                    out.append(f"{f.name}: {field}: PR number '{m.group()}' in prose, say what it changes")
            for rx in TOKENS:
                if m := rx.search(v):
                    out.append(f"{f.name}: {field}: token-shaped string '{m.group()[:4]}...'")
                    break
    return out


def main(argv):
    if len(argv) == 2 and argv[0] == "cards" and Path(argv[1]).is_dir():
        out = check_cards(argv[1])
    elif len(argv) == 1 and Path(argv[0]).is_file():
        out = check_page(Path(argv[0]).read_text(encoding="utf-8"))
    else:
        print("usage: check_page.py <page.html> | check_page.py cards <dir>  (path must exist)",
              file=sys.stderr)
        return 2
    print("\n".join(out) if out else "PASS")
    return 1 if out else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
