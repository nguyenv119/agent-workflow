"""Re-skin a document-skill page into the launch-note layout. Words stay; only CSS and page furniture change.
usage: python3 restyle.py <page.html> [...]
Rerunnable: on a page it already restyled it rebuilds the contents list, rail and change links.
It drops every <style> block, so a page with its own components (tiles, timelines, custom tables) loses their look:
restyle only pages whose classes this CSS covers, and diff the words before and after."""
import html, re, sys

FONTS = ('<link rel="preconnect" href="https://fonts.googleapis.com">\n'
         '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Source+Serif+4:opsz,wght@8..60,400;8..60,500;8..60,600'
         '&family=DM+Sans:wght@400;500;600&family=JetBrains+Mono:wght@400;500&display=swap">')

CSS = r"""<style>
/* launch-note layout (19 Oct): dark horizon hero with a numbered contents list, one 640px serif column on ivory,
   diagrams and tables break out to 880px. Ink means "already true"; clay means "changed in the latest update", in text
   and in diagrams. A layer's own boxes are stone grey, so clay keeps that one meaning. Single light look by choice. */
:root {
  --ivory: #faf9f5; --paper: #fff; --ink: #141413; --muted: #5e5d59; --hair: #d8d6cc; --code: #efede4;
  --clay: #d97757; --clay-ink: #b4532f; --clay-wash: #f0d3c4; --stone-wash: #d6d4ca; --night: #17130f;
  --serif: "Source Serif 4", Georgia, "Times New Roman", serif;
  --sans: "DM Sans", "Helvetica Neue", Arial, sans-serif;
  --mono: "JetBrains Mono", ui-monospace, Menlo, monospace;
  /* one type scale; every text size on the page is one of these */
  --t-label: .6875rem; --t-small: .8125rem; --t-body: 1.0625rem; --t-h3: 1.375rem; --t-lead: 1.625rem; --t-h2: 2rem;
  color-scheme: light;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--ivory); color: var(--ink); font: var(--t-body)/1.6 var(--serif); letter-spacing: -0.01em; -webkit-font-smoothing: antialiased; }
.wrap { padding-bottom: 6rem; }
.wrap > section, .wrap > .intro, .wrap > details.agents, .wrap > footer {
  max-width: calc(640px + 2 * max(16px, env(safe-area-inset-left, 0px))); margin-inline: auto;
  padding-left: max(16px, env(safe-area-inset-left, 0px)); padding-right: max(16px, env(safe-area-inset-right, 0px)); }

/* hero */
header.hero { color: #f4efe6; text-align: center; padding-block: 6rem 5rem;
  padding-left: max(16px, env(safe-area-inset-left, 0px)); padding-right: max(16px, env(safe-area-inset-right, 0px));
  /* the horizon is a fixed 620px band so a long contents list never pushes it out of view */
  background:
    linear-gradient(180deg, #0d1626 0px, #23446b 120px, #5a86b8 230px, #9aa6c4 300px, #d08a5c 350px, #d2522a 372px, #3a1d12 410px, var(--night) 520px) top / 100% 620px no-repeat,
    var(--night); }
.hero .kicker { display: none; }
.hero h1 { font: 500 clamp(2.3rem, 7vw, 4.4rem)/1.05 var(--serif); letter-spacing: -0.02em; margin: 0 auto 3.8rem; max-width: 15ch; text-wrap: balance; }
.toc { list-style: none; margin: 0 auto; padding: 0; width: min(30rem, 100%); display: grid; gap: .35rem; font: var(--t-small)/1.3 var(--serif); text-align: left; }
.toc a { padding-block: .15rem; display: flex; align-items: baseline; gap: .5rem; color: inherit; text-decoration: none; }
.toc a::after { content: ""; order: 1; flex: 1; min-width: 1.5rem; border-bottom: 1px dotted rgba(244,239,230,.45); transform: translateY(-.25em); }
.toc .n { font: var(--t-small) var(--sans); order: 0; flex: none; min-width: 2.2rem; }
.toc .t { order: 2; text-align: right; }
.toc a:hover .t, .toc a:focus-visible .t { text-decoration: underline; }
.toc-aux { margin: 1.4rem auto 0; font: var(--t-small)/1.4 var(--sans); display: flex; gap: .4rem 1.2rem; justify-content: center; flex-wrap: wrap; }
.toc-aux a { color: inherit; opacity: .85; }

/* contents rail: a copy of the hero list, pinned in the left margin once the hero scrolls away (wide screens only) */
.rail { display: none; }
/* it sits left of the 880px diagram column with a 24px gap, so it only shows where that space exists */
@media (min-width: 1360px) {
  .rail { display: block; position: fixed; top: 5rem; left: calc(50% - 440px - 24px - 200px); width: 200px; z-index: 3;
    opacity: 0; pointer-events: none; transition: opacity .2s ease; }
  .rail.on { opacity: 1; pointer-events: auto; }
  .rail ol { list-style: none; margin: 0; padding: 0; display: grid; gap: .1rem; font: var(--t-small)/1.35 var(--sans); }
  .rail a { display: flex; gap: .5rem; padding: .25rem .4rem; color: var(--muted); text-decoration: none; border-left: 2px solid transparent; }
  .rail a .n { flex: none; min-width: 2rem; }
  .rail a:hover { color: var(--ink); }
  .rail a.here { color: var(--ink); border-left-color: var(--ink); }
  .rail a.chg { color: var(--clay-ink); }
}

/* what changed: one strip under the standfirst, linking the layers that carry clay text */
.legend .new { display: block; border-left: 2px solid var(--clay); padding: .35rem 0 .35rem .8rem; background: linear-gradient(90deg, rgba(240,211,196,.55), rgba(240,211,196,0)); }
.legend .new a { color: var(--clay-ink); margin-left: .35rem; }

/* intro, column */
.intro { padding-top: 4.5rem; display: grid; gap: 1rem; }
.standfirst { font-size: var(--t-lead); line-height: 1.3; margin: 0; text-wrap: pretty; }
.altlink { font: var(--t-small)/1.4 var(--sans); margin: 0; }
.legend { display: flex; flex-wrap: wrap; gap: .5rem 1.2rem; align-items: center; font: var(--t-small)/1.5 var(--sans); color: var(--muted); }
.legend span.t { color: var(--muted); }
p { margin: 0 0 1rem; }
b, strong { font-weight: 600; }
h2 { font: 500 var(--t-h2)/1.2 var(--serif); margin: 0 0 1rem; text-wrap: balance; }
h3 { font: 500 var(--t-h3)/1.25 var(--serif); margin: 0 0 .8rem; text-wrap: balance; }
h3 .ln { display: block; font: 600 var(--t-label)/1.4 var(--sans); letter-spacing: .12em; text-transform: uppercase; color: var(--muted); margin-bottom: .35rem; }
.layer:has(.new) h3 .ln { color: var(--clay-ink); }
.layer:has(.new) h3 .ln::after { content: " \00b7  changed in this update"; }
h4 { font: 600 var(--t-body)/1.3 var(--serif); margin: 1.2rem 0 .4rem; }
section.block { padding-top: 4.5rem; }
a { color: inherit; text-decoration: underline; text-underline-offset: 2px; }
a.inline { color: inherit; }
code { font: .84em var(--mono); background: var(--code); padding: .05em .3em; border-radius: 3px; overflow-wrap: anywhere; }
pre { font: var(--t-small)/1.55 var(--mono); background: var(--code); border-radius: 6px; padding: .8rem 1rem; margin: 0 0 1rem; overflow-x: auto; }
pre code { background: none; padding: 0; }
ul, ol { padding-left: 1.3rem; margin: 0 0 1rem; }
li { margin: .3rem 0; }
.mono { font-family: var(--mono); font-size: .85em; }
.nw { white-space: nowrap; }

/* layers: a hairline and a run-in rhythm, no cards */
.layer { margin-top: 4rem; }
.pain b, .done b { font-weight: 600; }
.done { border-left: 2px solid var(--ink); padding-left: .9rem; }

/* status pills (internal furniture) */
.st { display: inline-block; font: 600 var(--t-label)/1.5 var(--sans); letter-spacing: .08em; text-transform: uppercase; padding: 0 .4rem; border: 1px solid var(--ink); color: var(--ink); vertical-align: .12em; white-space: nowrap; }
.st.soon { border-style: dashed; color: var(--muted); border-color: var(--muted); }
.st:not(.live):not(.soon) { background: var(--stone-wash); border-color: var(--stone-wash); }

/* change marking: clay = this update only */
.new, .new :is(p,td,h3,h4,li,i,b,.note,.w), ol.steps p.new, .new ol.steps :is(p,h4) { color: var(--clay-ink); }
.sw { display: inline-block; width: 10px; height: 10px; background: var(--clay-wash); outline: 1.5px solid var(--clay); vertical-align: 0; margin-right: .3rem; }

/* diagrams: break out of the column; ink outlines on white, square corners */
figure.dia { width: min(880px, 100vw - 32px); position: relative; left: 50%; transform: translateX(-50%); margin: 1.6rem 0; }
figure.dia .scroller { overflow-x: auto; }
figure.dia svg { display: block; width: 100%; min-width: 620px; height: auto; margin-inline: auto; }
figure.dia figcaption { max-width: 640px; margin: .7rem auto 0; font: var(--t-small)/1.5 var(--sans); color: var(--muted); }
.ent rect { rx: 0; ry: 0; }
/* earlier layers' boxes recede to hairline grey; this layer's boxes are the only ones in full ink */
.ent .bx { fill: var(--ivory); stroke: #b9b7ad; stroke-width: 1; }
.ent .bx.acc { fill: var(--paper); stroke: var(--ink); stroke-width: 1.5; }
.ent .lb { font: 500 10.5px var(--mono); letter-spacing: .06em; fill: #9b998f; }
.ent .lb.acc { fill: var(--muted); }
.ent .it { font: 11.5px var(--sans); fill: #8a887f; letter-spacing: 0; transform: translateY(-3px); }
.ent .it.acc { fill: var(--ink); }
.ent .ar { stroke: #b9b7ad; stroke-width: 1; fill: none; }
.ent .ar.acc { stroke: var(--ink); stroke-width: 1.5; }
.ent .cap { font: italic 12px var(--serif); fill: var(--muted); }
.ent .chg .bx, .ent .bx.chg { fill: var(--clay-wash); stroke: var(--clay); stroke-width: 1.6; }
.ent .ar.chg { stroke: var(--clay); stroke-width: 1.8; }
.ent .lb.chg, .ent .it.chg { fill: var(--clay-ink); }

/* tables */
.scroller { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; min-width: 520px; font: var(--t-small)/1.4 var(--sans); background: var(--ivory); margin: 0 0 1rem; }
th, td { border: 1px solid var(--ink); padding: .55rem .65rem; text-align: left; vertical-align: top; }
th { font-weight: 600; }

/* older page furniture: per-update notes, per-layer to-do boxes, plain lists, progress bars; neutral, so clay keeps its one meaning */
.upd { border-left: 2px solid var(--hair); padding: .2rem 0 .2rem .9rem; margin: 0 0 1.2rem; font: var(--t-small)/1.55 var(--sans); color: var(--muted); }
.upd.new, .new .upd { border-left-color: var(--clay); }
.ltodo { margin: 1.6rem 0 0; padding-top: .9rem; border-top: 1px solid var(--hair); font: var(--t-small)/1.55 var(--sans); }
.ltodo h4 { font: 600 var(--t-label)/1.4 var(--sans); letter-spacing: .12em; text-transform: uppercase; color: var(--muted); margin: 0 0 .6rem; }
.ltodo p { margin: 0 0 .7rem; }
ul.plain { padding-left: 1.1rem; }
.fillbar { height: 6px; background: var(--stone-wash); }
.fillbar > * { display: block; height: 100%; background: var(--ink); }

/* For agents: closed, plain */
details.agents { margin-top: 6rem; }
details.agents > summary { list-style: none; cursor: pointer; display: flex; gap: .7rem; align-items: baseline; flex-wrap: wrap; }
details.agents > summary::-webkit-details-marker { display: none; }
details.agents > summary h2 { margin: 0; }
details.agents h3 { margin-top: 2.5rem; }
.tri { font-size: var(--t-label); color: var(--muted); transition: transform .13s ease; }
details[open] > summary .tri { transform: rotate(90deg); }
.hint { font: var(--t-small) var(--sans); color: var(--muted); }
footer { margin-top: 4rem; font: var(--t-small)/1.5 var(--sans); color: var(--muted); }

/* edit shell */
.edit-bar { position: fixed; right: max(16px, env(safe-area-inset-right, 0px)); bottom: max(16px, env(safe-area-inset-bottom, 0px)); z-index: 5; display: flex; gap: 8px; align-items: center; }
.edit-bar button { font: 600 var(--t-small)/1 var(--sans); padding: .6rem .9rem; border: 1px solid var(--ink); background: var(--ivory); color: var(--ink); cursor: pointer; }
.edit-bar button.on { border-color: var(--clay); color: var(--clay-ink); }
.edit-bar .msg { font: var(--t-small) var(--sans); color: var(--muted); }
@media print { .edit-bar { display: none !important; } }
@media (prefers-reduced-motion: reduce) { * { transition: none !important; } }
</style>"""


def short(title):
    t = re.sub(r"^Layer (\w+)\.\s*", "", title)
    t = re.split(r"[,:;]| and (?=[a-z])", t)[0].strip()
    return t[:60]


def _center(svg):
    # crop the viewBox to what is drawn, so a three-box diagram sits centred instead of hugging the left edge
    vb = re.search(r'viewBox="[\d.]+ ([\d.]+) [\d.]+ ([\d.]+)"', svg)
    if not vb or 'style="max-width' in svg[:400]: return svg
    xs = []
    for x, w in re.findall(r'<rect[^>]*?\bx="([\d.]+)"[^>]*?\bwidth="([\d.]+)"', svg): xs += [float(x), float(x) + float(w)]
    for x1, x2 in re.findall(r'<line[^>]*?x1="([\d.]+)"[^>]*?x2="([\d.]+)"', svg): xs += [float(x1), float(x2)]
    if not xs: return svg
    lo, hi, y, H = max(0, min(xs) - 8), max(xs) + 8, vb.group(1), vb.group(2)
    w = hi - lo
    return svg.replace(vb.group(0), f'viewBox="{lo:g} {y} {w:g} {H}" style="max-width:{round(w * 1.16)}px;min-width:{min(round(w * 1.16), 620)}px"', 1)


def _finish(s, chg_links, rail_js):
    s = re.sub(r'\s*<p class="kicker">.*?</p>', "", s, count=1, flags=re.S)
    s = re.sub(r'<svg[^>]*viewBox="[^"]*".*?</svg>', lambda m: _center(m.group(0)) if 'class="bx' in m.group(0) else m.group(0), s, flags=re.S)
    # changed-layer links go at the end of the legend's update line; the rail script before </body>
    s = re.sub(r'(<div class="legend">.*?<span class="new">.*?)(</span>\s*</div>)', lambda m: m.group(1) + chg_links + m.group(2), s, count=1, flags=re.S)
    return s.replace("</body>", rail_js + "\n</body>", 1) if "</body>" in s else s + rail_js


def reskin(path):
    s = open(path).read()
    again = "launch-note layout" in s
    if again:
        s = re.sub(r'<ol class="toc">.*?</ol>\s*(<p class="toc-aux">.*?</p>\s*)?', "", s, flags=re.S)
        s = re.sub(r'<nav class="rail".*?</nav>\s*', "", s, flags=re.S)
        s = re.sub(r'<script>\s*/\* rail \*/.*?</script>\s*', "", s, flags=re.S)
        s = re.sub(r'\s*<a class="chg-link"[^>]*>[^<]*</a>', "", s)
    s = re.sub(r"<style>.*?</style>\s*", "", s, flags=re.S)
    s = re.sub(r'<link rel="stylesheet" href="https://fonts.googleapis.com[^>]*>\s*', "", s)
    # FONTS re-adds the preconnect line, so drop the old one first or every rerun stacks another copy
    s = re.sub(r'<link rel="preconnect" href="https://fonts\.(googleapis|gstatic)\.com"[^>]*>\s*', "", s)
    s = re.sub(r'<canvas id="(dither|halo)"[^>]*></canvas>\s*', "", s)
    s = re.sub(r"<script>\s*/\* Dither field.*?</script>\s*", "", s, flags=re.S)
    s = re.sub(r'<div class="top">.*?</div>\s*</div>\s*', "", s, count=1, flags=re.S)
    s = s.replace("<title>", FONTS + "\n" + CSS + "\n<title>", 1) if "<title>" in s else FONTS + CSS + s

    # ids on section h2s, then the contents list
    used = set(re.findall(r'id="([^"]+)"', s))
    def add_id(m):
        if "id=" in m.group(1): return m.group(0)
        base = re.sub(r"[^a-z0-9]+", "-", html.unescape(m.group(2)).lower()).strip("-")[:24] or "s"
        i, k = base, 2
        while i in used: i, k = f"{base}-{k}", k + 1
        used.add(i)
        return f'<h2{m.group(1)} id="{i}">{m.group(2)}</h2>'
    s = re.sub(r"<h2([^>]*)>([^<]+)</h2>", add_id, s)
    def add_lid(m):
        if "id=" in m.group(1): return m.group(0)
        n = re.match(r"Layer (\w+)\.", m.group(2))
        if not n: return m.group(0)
        i = "layer-" + n.group(1).lower(); used.add(i)
        return f'<h3{m.group(1)} id="{i}">{m.group(2)}'
    s = re.sub(r"<h3([^>]*)>(Layer [^<]+)", add_lid, s)
    # layer number becomes an eyebrow above the title (same words, own line)
    s = re.sub(r'(<h3[^>]*id="layer-[^"]+"[^>]*>)(Layer \w+\.)\s*', r'\1<span class="ln">\2</span>', s)

    # which layers carry clay text in this update
    changed = []
    for lm in re.finditer(r'<div class="layer[^"]*">(.*?)(?=<div class="layer|</section>)', s, flags=re.S):
        hid = re.search(r'id="(layer-[^"]+)"', lm.group(1))
        if hid and ('class="new' in lm.group(0) or ' new"' in lm.group(0)):
            changed.append(hid.group(1))

    toc, aux, n = [], [], 0
    for tag, attrs, text in re.findall(r"<(h2|h3)([^>]*)>(.*?)</\1>", s, flags=re.S):
        text = html.unescape(re.sub(r"<[^>]+>", "", text)).strip()
        idm = re.search(r'id="([^"]+)"', attrs)
        if not idm: continue
        if tag == "h3" and not text.startswith("Layer "): continue
        if tag == "h2" and text.startswith("How it works"): continue
        if tag == "h3":
            num = "(" + re.match(r"Layer (\w+)", text).group(1) + ")"
            label = html.escape(short(text))
            toc.append((idm.group(1), num, label))
        else:
            aux.append(f'<a href="#{idm.group(1)}">{text.split(":")[0]}</a>')
    if not toc:  # no layers: the plain sections are the contents
        toc = [(re.search(r'href="#([^"]+)"', a).group(1), f"({i})", re.sub(r"<[^>]+>", "", a)) for i, a in enumerate(aux, 1)]
        aux = []
    lis = "".join(f'<li><a href="#{i}"><span class="n">{n_}</span><span class="t">{t}</span></a></li>' for i, n_, t in toc)
    toc_html = '<ol class="toc">' + lis + "</ol>" + (f'\n    <p class="toc-aux">{" ".join(aux)}</p>' if aux else "")
    rail = '<nav class="rail" aria-label="Contents"><ol>' + "".join(
        f'<li><a href="#{i}"{" class=chg" if i in changed else ""}><span class="n">{n_}</span><span>{t}</span></a></li>' for i, n_, t in toc) + "</ol></nav>"
    rail_js = """<script>/* rail */(function(){var r=document.querySelector('.rail'),h=document.querySelector('header.hero');if(!r||!h||!window.IntersectionObserver)return;
new IntersectionObserver(function(e){r.classList.toggle('on',!e[0].isIntersecting)}).observe(h);
var links=[].slice.call(r.querySelectorAll('a')),io=new IntersectionObserver(function(es){es.forEach(function(x){if(x.isIntersecting){links.forEach(function(a){a.classList.toggle('here',a.getAttribute('href')==='#'+x.target.id)})}})},{rootMargin:'0px 0px -70% 0px'});
links.forEach(function(a){var t=document.getElementById(a.getAttribute('href').slice(1));if(t)io.observe(t)});})();</script>"""
    chg_links = "".join(f' <a class="chg-link" href="#{i}">{dict((x[0], x[1]) for x in toc)[i]}</a>' for i in changed)

    # hero keeps kicker + h1 + contents; the rest of the old header becomes the intro column
    if again:
        s = re.sub(r'(<header class="hero">.*?</h1>)', lambda m: m.group(1) + "\n    " + toc_html, s, count=1, flags=re.S)
        s = s.replace("</header>", "</header>\n  " + rail, 1)
        s = _finish(s, chg_links, rail_js)
        open(path, "w").write(s); print("refreshed:", path, "layers:", len(toc), "changed:", changed); return
    m = re.search(r'<header class="hero">(.*?)</header>', s, flags=re.S)
    inner = m.group(1)
    kicker = re.search(r'<p class="kicker">.*?</p>', inner, flags=re.S).group(0)
    h1 = re.search(r"<h1>.*?</h1>", inner, flags=re.S).group(0)
    rest = inner.replace(kicker, "").replace(h1, "").strip()
    s = s[:m.start()] + f'<header class="hero">\n    {kicker}\n    {h1}\n    {toc_html}\n  </header>\n  <div class="intro">\n    {rest}\n  </div>' + s[m.end():]
    s = s.replace("</header>", "</header>\n  " + rail, 1)
    s = _finish(s, chg_links, rail_js)
    open(path, "w").write(s)
    print("reskinned:", path, "contents entries:", len(toc))


if __name__ == "__main__":
    for p in sys.argv[1:]:
        reskin(p)
