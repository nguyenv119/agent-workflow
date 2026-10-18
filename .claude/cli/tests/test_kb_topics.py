"""Tests for .claude/skills/discover/kb_topics.py route: when the topic prefilter runs."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import tempfile
import unittest
from pathlib import Path

from helpers import CLAUDE

SCRIPT = CLAUDE / "skills/discover/kb_topics.py"
spec = importlib.util.spec_from_file_location("kb_topics", SCRIPT)
kb_topics = importlib.util.module_from_spec(spec)
spec.loader.exec_module(kb_topics)


def fake_ask(calls):
    """Stand-in for the Jev call: records each request's state and answers every question."""
    def ask(key, state, questions):
        calls.append(state)
        out = {}
        for q, spec_ in questions.items():
            if spec_["type"] == "choice":
                out[q] = {"probabilities": {i: 0.0 for i in spec_["criteria"]}}
            else:
                out[q] = {"noul": 0.9}
        return out
    return ask


class RoutePrefilterTest(unittest.TestCase):
    def route(self, n):
        with tempfile.TemporaryDirectory() as d:
            kb = Path(d)
            for i in range(n):
                tag = "alpha" if i % 2 else "beta"
                (kb / f"n{i:04d}.md").write_text(f"# note {i}\n\nTopics: {tag}\n\nbody {i}\n")
            calls = []
            real = kb_topics.ask
            kb_topics.ask = fake_ask(calls)
            try:
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    kb_topics.route(kb, "k", {"alpha": "a", "beta": "b"}, "q")
            finally:
                kb_topics.ask = real
            return calls, out.getvalue()

    def test_446_notes_rank_every_note(self):
        calls, out = self.route(446)
        self.assertFalse([c for c in calls if set(c) == {"question"}], "topic prefilter ran at 446 notes")
        self.assertIn("ranked 446 of 446", out)

    def test_prefilter_returns_past_the_ceiling(self):
        calls, out = self.route(kb_topics.FILTER_ABOVE + 1)
        self.assertTrue([c for c in calls if set(c) == {"question"}], "topic prefilter did not run")
        self.assertTrue(out.startswith("topics: "))


if __name__ == "__main__":
    unittest.main()
