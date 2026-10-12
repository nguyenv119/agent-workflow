"""Pick candidate PRs for the fresh exam task set. Free: GitHub API only, no LLM.

Usage: python3 pick_prs.py --from 2026-08-01 --to 2026-10-31
           [--issue-since 2026-07-01] [--target 45] [--repos-per-language 150] [--out FILE]

Two stages, because GitHub's PR search cannot filter on stars:
1. `gh search repos`: Python / TypeScript repos with >= 500 stars pushed since --from.
2. `gh api graphql`: per repo, merged PRs in the window that close an issue, with
   their files, labels and linked issues.
Then every filter below runs locally, at most 3 PRs per repo are kept, and the pick is
balanced across (language x kind). Writes candidates-<YYYY-MM of --to>.json next to
this file: public PR metadata only.
"""
import argparse
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

LANGS = {"Python": "python", "TypeScript": "typescript"}  # GitHub primaryLanguage -> ours
MIN_STARS = 500
MAX_PER_REPO = 3
MIN_SOURCE, MAX_SOURCE = 3, 10  # SWE-gen's own create defaults (config.py:97-98)
FILES_FETCHED = 60  # PRs touching more files than this are too large to be one task
PRS_PER_REPO = 15   # newest merged PRs fetched per repo
REPOS_PER_QUERY = 5
SOURCE_EXT = (".py", ".pyi", ".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")
TEST_DIR = re.compile(r"(^|/)(tests?|__tests__|spec|testing)/")
TEST_FILE = re.compile(r"(^|/)(test_[^/]+\.py|[^/]+_test\.py|conftest\.py|[^/]+\.(test|spec)\.[cm]?[jt]sx?)$")
BUG_LABEL = re.compile(r"\b(bug|bugfix|fix|regression|defect|crash)\b", re.I)

PR_FIELDS = """
  ... on PullRequest {
    number title url mergedAt
    labels(first: 20) { nodes { name } }
    repository { nameWithOwner stargazerCount isFork primaryLanguage { name } }
    closingIssuesReferences(first: 5) { nodes { number createdAt labels(first: 20) { nodes { name } } } }
    files(first: %d) { totalCount nodes { path } }
  }""" % FILES_FETCHED


def gh(args):
    """Run `gh <args>` and return parsed JSON stdout; raise with gh's stderr on failure."""
    p = subprocess.run(["gh", *args], capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:3])} failed: {p.stderr.strip()}")
    return json.loads(p.stdout)


def is_test_path(path):
    return bool(TEST_DIR.search(path) or TEST_FILE.search(path))


def is_source_path(path):
    return path.endswith(SOURCE_EXT) and not is_test_path(path)


def search_repos(language, since, limit, run=gh):
    """Repos with >= MIN_STARS stars, this primary language, pushed on/after `since`."""
    rows = run(["search", "repos", "--language", language, "--stars", f">={MIN_STARS}",
                "--updated", f">={since}", "--archived=false", "--sort", "updated",
                "--limit", str(limit), "--json", "fullName"])
    return [r["fullName"] for r in rows]


def fetch_prs(repos, start, end, run=gh):
    """Merged PRs in [start, end] that link an issue, for each repo (GraphQL, batched)."""
    nodes = []
    for i in range(0, len(repos), REPOS_PER_QUERY):
        batch = repos[i:i + REPOS_PER_QUERY]
        fields = "\n".join(
            f"r{j}: search(type: ISSUE, first: {PRS_PER_REPO}, query: "
            f"{json.dumps(f'repo:{r} is:pr is:merged merged:{start}..{end} linked:issue sort:updated-desc')})"
            f" {{ nodes {{ {PR_FIELDS} }} }}"
            for j, r in enumerate(batch))
        data = run(["api", "graphql", "-f", f"query=query {{ {fields} }}"])
        for result in data["data"].values():
            nodes.extend(n for n in result["nodes"] if n)
    return nodes


def kind_of(pr):
    """bugfix if the PR or a linked issue has a bug-like label, else feature."""
    names = [l["name"] for l in pr["labels"]["nodes"]]
    for issue in pr["closingIssuesReferences"]["nodes"]:
        names += [l["name"] for l in issue["labels"]["nodes"]]
    return "bugfix" if any(BUG_LABEL.search(n.replace("-", " ").replace(":", " ")) for n in names) else "feature"


def check(pr, start, end, issue_since):
    """Return (row, None) if the PR passes every filter, else (None, reason)."""
    repo = pr["repository"]
    lang = LANGS.get((repo.get("primaryLanguage") or {}).get("name"))
    if lang is None:
        return None, "language"
    if repo["isFork"] or repo["stargazerCount"] < MIN_STARS:
        return None, "stars"
    if not pr.get("mergedAt") or not start <= pr["mergedAt"][:10] <= end:
        return None, "merged_window"
    issues = pr["closingIssuesReferences"]["nodes"]
    if not issues:
        return None, "no_issue"
    # Every linked issue must be new: an old issue's text may be in the training data.
    if min(i["createdAt"][:10] for i in issues) < issue_since:
        return None, "issue_too_old"
    files = pr["files"]
    if files["totalCount"] > len(files["nodes"]):
        return None, "too_many_files"
    paths = [f["path"] for f in files["nodes"]]
    tests = [p for p in paths if is_test_path(p)]
    n_source = sum(is_source_path(p) for p in paths)
    if not tests:
        return None, "no_tests"
    if not MIN_SOURCE <= n_source <= MAX_SOURCE:
        return None, "source_files"
    return {
        "repo": repo["nameWithOwner"], "pr": pr["number"], "url": pr["url"], "title": pr["title"],
        "merged_at": pr["mergedAt"], "language": lang, "kind": kind_of(pr),
        "stars": repo["stargazerCount"], "issues": sorted(i["number"] for i in issues),
        "issue_created_at": min(i["createdAt"] for i in issues),
        "source_files": n_source, "test_files": sorted(tests),
    }, None


def select(rows, target, per_repo=MAX_PER_REPO):
    """Round-robin over (language, kind) buckets, at most per_repo PRs per repo.

    Within a bucket, each repo's newest PR comes before any repo's second one, so
    the pick spreads over repos before it doubles up.
    """
    rank = Counter()
    keyed = []
    for r in sorted(rows, key=lambda r: (r["repo"], r["merged_at"]), reverse=True):
        keyed.append(((rank[r["repo"]], r["repo"], r["pr"]), r))
        rank[r["repo"]] += 1
    buckets = {}
    for _, r in sorted(keyed, key=lambda kr: kr[0]):
        buckets.setdefault((r["language"], r["kind"]), []).append(r)
    order = sorted(buckets)
    taken, per = [], Counter()
    while len(taken) < target and any(buckets[b] for b in order):
        for b in order:
            while buckets[b]:
                r = buckets[b].pop(0)
                if per[r["repo"]] < per_repo:
                    taken.append(r)
                    per[r["repo"]] += 1
                    break
            if len(taken) >= target:
                break
    return taken


def split(rows):
    lang = Counter(r["language"] for r in rows)
    kind = Counter(r["kind"] for r in rows)
    return (f"{lang['python']} python, {lang['typescript']} typescript; "
            f"{kind['bugfix']} bugfix, {kind['feature']} feature")


def pick(start, end, issue_since, target, repos_per_language, run=gh):
    """Return (selected rows, rejection counts, eligible count)."""
    repos = []
    for language in LANGS:
        repos += search_repos(language, start, repos_per_language, run)
    rows, reasons = [], Counter()
    for pr in fetch_prs(list(dict.fromkeys(repos)), start, end, run):
        row, why = check(pr, start, end, issue_since)
        if row:
            rows.append(row)
        else:
            reasons[why] += 1
    unique = list({(r["repo"], r["pr"]): r for r in rows}.values())
    return select(unique, target), reasons, len(unique)


def main(argv=None, run=gh):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--from", dest="start", required=True)
    ap.add_argument("--to", dest="end", required=True)
    ap.add_argument("--issue-since", default="2026-07-01")
    ap.add_argument("--target", type=int, default=45)
    ap.add_argument("--repos-per-language", type=int, default=150)
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    for d in (a.start, a.end, a.issue_since):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
            ap.error(f"dates must be YYYY-MM-DD, got {d!r}")
    out = Path(a.out) if a.out else Path(__file__).resolve().parent / f"candidates-{a.end[:7]}.json"
    chosen, reasons, eligible = pick(a.start, a.end, a.issue_since, a.target, a.repos_per_language, run)
    out.write_text(json.dumps({
        "window": {"from": a.start, "to": a.end}, "issue_since": a.issue_since,
        "filters": {"min_stars": MIN_STARS, "max_per_repo": MAX_PER_REPO,
                    "source_files": [MIN_SOURCE, MAX_SOURCE], "languages": sorted(LANGS.values())},
        "candidates": chosen,
    }, indent=2) + "\n")
    print(f"{len(chosen)} candidates ({split(chosen)}) -> {out.name}")
    print(f"  eligible {eligible}; rejected: "
          + ", ".join(f"{k} {n}" for k, n in reasons.most_common()), file=sys.stderr)


if __name__ == "__main__":
    main()
