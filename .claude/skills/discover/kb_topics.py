#!/usr/bin/env python3
"""Topic tags for a discover knowledge base, judged by Jev (TypeSafe).

  kb_topics.py route KB "question"   which topics fit, then the notes worth reading first
  kb_topics.py tag   KB NOTE.md ...  tag notes (writes a "Topics:" line under the H1), then rebuild INDEX.md
  kb_topics.py tag   KB --all        tag every note in the index and the KB folder
  kb_topics.py index KB              rebuild INDEX.md grouped by each note's first topic

KB is the knowledge-base folder, e.g. .claude/discover-kb/<repo-slug>. It needs a topics.json
({"slug": "what the topic covers"}). Without one, or without TYPESAFE_API_KEY, route prints
INDEX.md and tag does nothing, so the skill falls back to reading the index by hand.
One note file per finding: a note with several topics is listed once, under its first topic,
and route still finds it through its other tags.
"""
import json, os, re, sys, urllib.request, urllib.error, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

API = "https://api.typesafe.ai/v1/systemone"
SKIP = {"INDEX.md", "ARCHIVE.md"}
MAX_CHOICE = 255  # a Jev Choice takes at most 255 options
FILTER_ABOVE = 255  # past this many notes, rank only notes in the matched topics
LINK = re.compile(r'^- \[(.+)\]\(([^)]+\.md)\)\s*$')


def load_key(kb):
    if os.environ.get("TYPESAFE_API_KEY"):
        return os.environ["TYPESAFE_API_KEY"]
    # ponytail: walk up from the KB and cwd looking for .claude/.env; add a config knob if a repo keeps it elsewhere
    for start in (Path(kb).resolve(), Path.cwd()):
        for d in (start, *start.parents):
            env = d / ".claude" / ".env"
            if env.is_file():
                m = re.search(r'^TYPESAFE_API_KEY=["\']?([^"\'\s]+)', env.read_text(errors="ignore"), re.M)
                if m:
                    return m.group(1)
    return None


def ask(key, state, questions):
    body = json.dumps({"model": "jev-latest", "state": state, "questions": questions}).encode()
    req = urllib.request.Request(API, body, {"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    for i in range(4):
        try:
            return json.load(urllib.request.urlopen(req, timeout=120))["answers"]
        except urllib.error.HTTPError as e:
            if e.code != 429 or i == 3:
                raise
            time.sleep(2 * (i + 1))


def read_index(kb):
    """[(label, link)] in index order; link is relative to kb (may point into a sibling KB)."""
    p = Path(kb) / "INDEX.md"
    if not p.exists():
        return []
    return [(m.group(1), m.group(2)) for m in map(LINK.match, p.read_text().splitlines()) if m]


def h1(path):
    for line in path.read_text(errors="ignore").splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return path.stem


def tags_of(path):
    m = re.search(r'^Topics:\s*(.+)$', path.read_text(errors="ignore"), re.M)
    return [t.strip() for t in m.group(1).split(",")] if m else []


def notes(kb):
    """{link: label} for every indexed note plus every loose note file in the KB folder."""
    kb = Path(kb)
    out = dict((link, label) for label, link in read_index(kb))
    arch = kb / "ARCHIVE.md"  # archived notes stay unlisted, as the skill intends
    archived = set(re.findall(r'\]\(([^)]+\.md)\)', arch.read_text())) if arch.exists() else set()
    for f in sorted(kb.glob("*.md")):
        if f.name not in SKIP and not f.name.startswith("HANDOFF") and f.name not in out and f.name not in archived:
            out[f.name] = h1(f)[:110]
    return out


def tag_one(key, topics, path):
    text = path.read_text(errors="ignore")
    body = re.sub(r'^Topics:.*\n', '', text, flags=re.M)
    body = re.sub(r'^(# .*\n)\n+', r'\1\n', body, count=1, flags=re.M)
    qs = {t: {"type": "noul", "instructions": {"topic": d,
          "question": "Is the research note in `note` mainly about `topic`, so someone looking into that topic should find it?"}}
          for t, d in topics.items()}
    a = ask(key, {"note": body[:2500]}, qs)
    p = sorted(((a[t]["noul"], t) for t in topics), reverse=True)
    chosen = [t for s, t in p if s >= 0.5][:3] or [p[0][1]]  # primary first; always at least one
    lines = body.splitlines(keepends=True)
    i = next((n for n, l in enumerate(lines) if l.startswith("# ")), -1)
    lines.insert(i + 1, "\nTopics: " + ", ".join(chosen) + "\n" if i >= 0 else "Topics: " + ", ".join(chosen) + "\n\n")
    path.write_text("".join(lines).replace("\n\n\nTopics:", "\n\nTopics:"))
    return chosen


def rebuild_index(kb, topics):
    kb = Path(kb)
    old = dict((link, label) for label, link in read_index(kb))
    groups = {t: [] for t in topics}
    groups["untagged"] = []
    for link, label in notes(kb).items():
        f = (kb / link)
        if not f.exists():
            continue
        t = (tags_of(f) or ["untagged"])[0]
        groups.setdefault(t, []).append(f"- [{old.get(link, label)}]({link})")
    out = [f"# Discover KB — {kb.name}", "",
           "One line per note, grouped by its first topic (topics.json). A note with several topics is listed",
           "once; `kb_topics.py route` finds it under all of them. Rebuilt by `kb_topics.py index`.", ""]
    for t, rows in groups.items():
        if rows:
            out += [f"## {t}", *rows, ""]
    (kb / "INDEX.md").write_text("\n".join(out))
    return sum(map(len, groups.values()))


def route(kb, key, topics, question, top=8):
    kb = Path(kb)
    qs = {t: {"type": "noul", "instructions": {"topic": d,
          "question": "Would research notes about `topic` likely help answer the question in `question`?"}}
          for t, d in topics.items()}
    a = ask(key, {"question": question}, qs)
    p = sorted(((a[t]["noul"], t) for t in topics), reverse=True)
    picked = [t for s, t in p if s >= 0.25][:4] or [p[0][1]]
    print("topics: " + ", ".join(f"{t} {s:.2f}" for s, t in p if t in picked))
    alln = [(link, label) for link, label in notes(kb).items() if (kb / link).exists()]
    # A topic filter drops the right note whenever the topic guess is wrong (27/44 vs 37/44 on the
    # 09-30 gold set), so rank the whole list while it fits in one Choice; filter only past that.
    cand = alln if len(alln) <= FILTER_ABOVE else [(l, t) for l, t in alln if set(tags_of(kb / l)) & set(picked)]
    if not cand:
        print("no tagged notes in those topics; read INDEX.md")
        return
    scores = {}
    for c in range(0, len(cand), MAX_CHOICE):
        chunk = cand[c:c + MAX_CHOICE]
        ids = {f"N{i:03d}": link for i, (link, _) in enumerate(chunk)}
        listing = "\n".join(f"N{i:03d}| {label}" for i, (_, label) in enumerate(chunk))
        r = ask(key, {"notes": listing, "question": question}, {"pick": {"type": "choice",
                "instructions": "Each line of `notes` is a research note: an ID, then its title. Which note would be most useful to read first to answer `question`?",
                "criteria": {i: None for i in ids}}})
        for i, pr in r["pick"]["probabilities"].items():
            scores[ids[i]] = pr
    label = dict(cand)
    print(f"notes (ranked {len(cand)} of {len(alln)}), best first:")
    for link in sorted(scores, key=lambda k: -scores[k])[:top]:
        print(f"  {scores[link]:.2f}  {kb / link}  {label[link][:90]}")


def main():
    if len(sys.argv) < 3 or sys.argv[1] not in ("route", "tag", "index"):
        sys.exit(__doc__)
    cmd, kb = sys.argv[1], Path(sys.argv[2])
    tf = kb / "topics.json"
    topics = json.loads(tf.read_text()) if tf.exists() else None
    key = load_key(kb)
    if cmd != "index" and not (topics and key):
        why = "no topics.json in " + str(kb) if not topics else "no TYPESAFE_API_KEY"
        print(f"kb_topics: {why}; read {kb / 'INDEX.md'} directly instead.")
        return
    if cmd == "index":
        if topics:
            print(f"index rebuilt: {rebuild_index(kb, topics)} notes")
        return
    if cmd == "route":
        return route(kb, key, topics, " ".join(sys.argv[3:]))
    targets = list(notes(kb)) if sys.argv[3:] == ["--all"] else sys.argv[3:]
    paths = [p if p.is_absolute() or p.exists() else kb / p for p in map(Path, targets)]
    with ThreadPoolExecutor(8) as ex:
        for p, t in zip(paths, ex.map(lambda p: tag_one(key, topics, p) if p.exists() else None, paths)):
            print(f"  {', '.join(t) if t else 'MISSING'}  {p.name}")
    print(f"index rebuilt: {rebuild_index(kb, topics)} notes")


if __name__ == "__main__":
    main()
