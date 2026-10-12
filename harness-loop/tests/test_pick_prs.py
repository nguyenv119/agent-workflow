"""Tests for harness-loop/tasks/fresh/pick_prs.py (free candidate-PR picker)."""
import copy
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures" / "pick_prs"
sys.path.insert(0, str(HERE.parent / "tasks" / "fresh"))
import pick_prs  # noqa: E402

START, END, SINCE = "2026-08-01", "2026-10-31", "2026-07-01"


def saved(name):
    return json.loads((FIXTURES / name).read_text())


def real_pr(number):
    """One PR node from the saved `gh api graphql` response."""
    for result in saved("graphql_prs.json")["data"].values():
        for n in result["nodes"]:
            if n["number"] == number:
                return copy.deepcopy(n)
    raise KeyError(number)


ELIGIBLE = 4842  # amd/gaia#4842: 3 source files, 1 test, issue labelled bug, merged 2026-10-06


def pr(**over):
    """The saved eligible PR with edits applied: merged_at, issue_at, stars, lang, paths."""
    n = real_pr(ELIGIBLE)
    if "merged_at" in over:
        n["mergedAt"] = over["merged_at"]
    if "issue_at" in over:
        for i in n["closingIssuesReferences"]["nodes"]:
            i["createdAt"] = over["issue_at"]
    if "stars" in over:
        n["repository"]["stargazerCount"] = over["stars"]
    if "lang" in over:
        n["repository"]["primaryLanguage"] = {"name": over["lang"]}
    if "paths" in over:
        n["files"] = {"totalCount": len(over["paths"]), "nodes": [{"path": p} for p in over["paths"]]}
    return n


def row(repo, number, language="python", kind="feature", merged="2026-09-01T00:00:00Z"):
    return {"repo": repo, "pr": number, "language": language, "kind": kind, "merged_at": merged}


class CheckTest(unittest.TestCase):
    def verdict(self, node):
        return pick_prs.check(node, START, END, SINCE)

    def test_saved_eligible_pr_passes_with_public_metadata(self):
        """
        A real PR that meets every filter becomes a candidate row carrying
        only public PR metadata the build step needs (repo, number, test file
        paths). If this broke, the real run would silently produce nothing.
        """
        # GIVEN the saved amd/gaia#4842 node
        # WHEN it is checked against the Aug-Oct window
        r, why = self.verdict(real_pr(ELIGIBLE))
        # THEN it passes as a python bugfix with its one test file listed
        self.assertIsNone(why)
        self.assertEqual((r["repo"], r["pr"], r["language"], r["kind"]),
                         ("amd/gaia", 4842, "python", "bugfix"))
        self.assertEqual(r["test_files"], ["tests/unit/chat/ui/test_memory_toggle_cached_agent.py"])
        self.assertEqual(r["source_files"], 3)

    def test_merged_july_31_is_excluded_and_august_1_included(self):
        """
        The window edge: a PR merged 2026-07-31 (late UTC) is before the
        window and could be in training data; one merged 2026-08-01 00:00 UTC
        is inside it.
        """
        # GIVEN the same PR merged at the last second of July and the first of August
        # WHEN each is checked
        _, before = self.verdict(pr(merged_at="2026-07-31T23:59:59Z"))
        r, after = self.verdict(pr(merged_at="2026-08-01T00:00:00Z"))
        # THEN July is rejected for the window and August passes
        self.assertEqual(before, "merged_window")
        self.assertIsNone(after)

    def test_merged_after_window_end_is_excluded(self):
        """A PR merged the day after --to is outside the window."""
        # GIVEN a PR merged 2026-11-01
        # WHEN it is checked
        _, why = self.verdict(pr(merged_at="2026-11-01T00:00:01Z"))
        # THEN it is rejected for the window
        self.assertEqual(why, "merged_window")

    def test_issue_created_june_30_is_excluded_and_july_1_included(self):
        """
        The linked issue's text becomes the task instruction, so an issue
        written before 2026-07-01 may be memorized: June 30 is out, July 1 in.
        """
        # GIVEN the PR with its issue created on each side of the cutoff
        # WHEN each is checked
        _, old = self.verdict(pr(issue_at="2026-06-30T23:59:59Z"))
        _, new = self.verdict(pr(issue_at="2026-07-01T00:00:00Z"))
        # THEN the June issue is rejected and the July one passes
        self.assertEqual(old, "issue_too_old")
        self.assertIsNone(new)

    def test_one_old_issue_among_several_rejects(self):
        """Every linked issue must be new; one old one is enough to reject."""
        # GIVEN a PR closing a new issue and an old one
        n = pr()
        old = copy.deepcopy(n["closingIssuesReferences"]["nodes"][0])
        old["number"], old["createdAt"] = 1, "2025-01-01T00:00:00Z"
        n["closingIssuesReferences"]["nodes"].append(old)
        # WHEN it is checked
        _, why = self.verdict(n)
        # THEN it is rejected as too old
        self.assertEqual(why, "issue_too_old")

    def test_no_linked_issue_is_excluded(self):
        """SWE-gen requires a linked issue; a PR with none is dropped."""
        # GIVEN a PR with no closing issue
        n = pr()
        n["closingIssuesReferences"]["nodes"] = []
        # WHEN it is checked
        _, why = self.verdict(n)
        # THEN it is rejected
        self.assertEqual(why, "no_issue")

    def test_stars_499_excluded_500_included(self):
        """The >= 500 star floor, on both sides."""
        # GIVEN the PR in a 499-star and a 500-star repo
        # WHEN each is checked
        _, low = self.verdict(pr(stars=499))
        _, ok = self.verdict(pr(stars=500))
        # THEN only the 500-star repo passes
        self.assertEqual(low, "stars")
        self.assertIsNone(ok)

    def test_other_language_excluded_typescript_included(self):
        """Only Python and TypeScript repos qualify; TypeScript maps to 'typescript'."""
        # GIVEN the PR in a JavaScript repo and in a TypeScript repo
        # WHEN each is checked
        _, js = self.verdict(pr(lang="JavaScript"))
        r, _ = self.verdict(pr(lang="TypeScript"))
        # THEN JavaScript is rejected and TypeScript passes as 'typescript'
        self.assertEqual(js, "language")
        self.assertEqual(r["language"], "typescript")

    def test_source_file_count_bounds_match_swegen(self):
        """
        SWE-gen itself rejects PRs outside 3-10 non-test source files; picking
        those would waste a paid build, so 2 and 11 are dropped, 3 and 10 kept.
        Docs and test files do not count as source.
        """
        def paths(n):
            return [f"pkg/m{i}.py" for i in range(n)] + ["tests/test_m.py", "README.md"]
        # GIVEN PRs with 2, 3, 10 and 11 source files
        # WHEN each is checked
        got = {n: self.verdict(pr(paths=paths(n)))[1] for n in (2, 3, 10, 11)}
        # THEN only 3 and 10 pass
        self.assertEqual(got, {2: "source_files", 3: None, 10: None, 11: "source_files"})

    def test_pr_without_test_files_is_excluded(self):
        """A PR touching no tests gives no hidden tests to grade with."""
        # GIVEN a PR with source files only
        # WHEN it is checked
        _, why = self.verdict(pr(paths=["a.py", "b.py", "c.py"]))
        # THEN it is rejected
        self.assertEqual(why, "no_tests")

    def test_more_files_than_fetched_is_excluded(self):
        """The saved amd/gaia#4839 touches 77 files, more than were fetched: too big."""
        # GIVEN the saved 77-file PR
        # WHEN it is checked
        _, why = self.verdict(real_pr(4839))
        # THEN it is rejected as too large rather than judged on a partial file list
        self.assertEqual(why, "too_many_files")

    def test_kind_feature_when_no_bug_label_anywhere(self):
        """Without a bug-like label on the PR or its issue, kind defaults to feature."""
        # GIVEN the PR with the issue's labels replaced by non-bug ones
        n = pr()
        n["closingIssuesReferences"]["nodes"][0]["labels"]["nodes"] = [{"name": "enhancement"}]
        # WHEN it is checked
        r, _ = self.verdict(n)
        # THEN it is a feature
        self.assertEqual(r["kind"], "feature")

    def test_kind_bugfix_from_pr_label_like_type_bug(self):
        """A PR label such as 'type: bug' makes it a bugfix; 'debug-tools' does not."""
        # GIVEN the PR with issue labels cleared and two different PR labels
        n = pr()
        n["closingIssuesReferences"]["nodes"][0]["labels"]["nodes"] = []
        n["labels"]["nodes"] = [{"name": "type: bug"}]
        m = copy.deepcopy(n)
        m["labels"]["nodes"] = [{"name": "debug-tools"}]
        # WHEN each is classified
        # THEN 'type: bug' is a bugfix and 'debug-tools' a feature
        self.assertEqual(pick_prs.kind_of(n), "bugfix")
        self.assertEqual(pick_prs.kind_of(m), "feature")


class PathTest(unittest.TestCase):
    def test_test_path_patterns(self):
        """Test detection covers Python and TS conventions and leaves source alone."""
        # GIVEN typical test and source paths
        tests = ["tests/unit/x.py", "src/test_a.py", "pkg/a_test.py", "src/a.test.ts",
                 "web/b.spec.tsx", "src/__tests__/c.ts", "conftest.py"]
        source = ["src/contest.py", "src/attest.ts", "src/latest/a.py", "src/testing_utils_x.py"]
        # WHEN each is classified
        # THEN tests are tests and source is not
        self.assertEqual([t for t in tests if not pick_prs.is_test_path(t)], [])
        self.assertEqual([s for s in source if pick_prs.is_test_path(s)], [])


class SelectTest(unittest.TestCase):
    def test_at_most_three_per_repo(self):
        """
        One busy repo must not fill the set: at most 3 PRs per repo, even
        when it has the only eligible PRs.
        """
        # GIVEN 6 eligible PRs, all from one repo
        rows = [row("a/a", i) for i in range(6)]
        # WHEN 45 are requested
        got = pick_prs.select(rows, 45)
        # THEN only 3 are taken
        self.assertEqual(len(got), 3)

    def test_balances_language_and_kind(self):
        """
        With plenty of every (language, kind) the pick is an even split, so
        the exam is not all one language or all bugfixes.
        """
        # GIVEN 10 PRs in each of the 4 buckets, each from its own repo
        rows = [row(f"{lang}/{kind}{i}", i, lang, kind)
                for lang in ("python", "typescript") for kind in ("bugfix", "feature")
                for i in range(10)]
        # WHEN 8 are requested
        got = pick_prs.select(rows, 8)
        # THEN each bucket gives 2
        buckets = sorted((r["language"], r["kind"]) for r in got)
        self.assertEqual(buckets, sorted([(l, k) for l in ("python", "typescript")
                                          for k in ("bugfix", "feature")] * 2))

    def test_short_bucket_is_topped_up_by_others(self):
        """When one bucket runs dry the others fill the target."""
        # GIVEN 1 typescript bugfix and 10 python features, all different repos
        rows = [row("t/b", 1, "typescript", "bugfix")] + [row(f"p/f{i}", i) for i in range(10)]
        # WHEN 5 are requested
        got = pick_prs.select(rows, 5)
        # THEN the 1 typescript bugfix and 4 python features are taken
        self.assertEqual(len(got), 5)
        self.assertEqual(sum(r["language"] == "typescript" for r in got), 1)

    def test_spreads_over_repos_before_doubling_up(self):
        """Each repo's newest PR is taken before any repo's second."""
        # GIVEN repo a with 3 PRs and repo b with 1, same bucket
        rows = [row("a/a", 1, merged="2026-08-01T00:00:00Z"), row("a/a", 2, merged="2026-09-01T00:00:00Z"),
                row("a/a", 3, merged="2026-10-01T00:00:00Z"), row("b/b", 9)]
        # WHEN 2 are requested
        got = pick_prs.select(rows, 2)
        # THEN one from each repo, a's newest
        self.assertEqual(sorted((r["repo"], r["pr"]) for r in got), [("a/a", 3), ("b/b", 9)])


class FakeGh:
    """Stands in for the `gh` CLI (mocked: external network); replays saved JSON."""

    def __init__(self):
        self.calls = []

    def __call__(self, args):
        self.calls.append(args)
        if args[:2] == ["search", "repos"]:
            return saved(f"search_repos_{args[args.index('--language') + 1].lower()}.json")
        if args[:2] == ["api", "graphql"]:
            return saved("graphql_prs.json")
        raise AssertionError(f"unexpected gh call {args}")


class MainTest(unittest.TestCase):
    def test_writes_candidates_file_and_prints_split(self):
        """
        End to end on saved gh output: the candidates file is written under
        the --to month name and the printed line gives count and split, which
        is what a human reads before any spend.
        """
        # GIVEN saved gh responses and a temp output path
        fake = FakeGh()
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "candidates-2026-10.json"
            buf = io.StringIO()
            # WHEN the picker runs
            with redirect_stdout(buf), redirect_stderr(io.StringIO()):
                pick_prs.main(["--from", START, "--to", END, "--out", str(out)], run=fake)
            doc = json.loads(out.read_text())
        # THEN amd/gaia's two eligible PRs are written and summarised
        self.assertEqual(sorted(c["pr"] for c in doc["candidates"]), [4842, 4974])
        self.assertEqual(doc["window"], {"from": START, "to": END})
        self.assertIn("2 candidates (2 python, 0 typescript; 2 bugfix, 0 feature)", buf.getvalue())
        # AND the repo search asked GitHub for >= 500 stars and the PR search used the window
        repo_call = fake.calls[0]
        self.assertIn(">=500", repo_call)
        query = next(c for c in fake.calls if c[:2] == ["api", "graphql"])[-1]
        self.assertIn(f"merged:{START}..{END}", query)
        self.assertIn("linked:issue", query)

    def test_rejects_malformed_date(self):
        """A non-ISO date is refused before any gh call."""
        # GIVEN a fake gh and a bad --from
        fake = FakeGh()
        # WHEN the picker runs
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            pick_prs.main(["--from", "2026/08/01", "--to", END], run=fake)
        # THEN gh was never called
        self.assertEqual(fake.calls, [])


if __name__ == "__main__":
    unittest.main()
