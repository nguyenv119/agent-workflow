"""Tests for the eval verdict libraries (eval_lib/verdict.sh and verdict.py), run as real scripts."""
import os
import re
import subprocess
import sys
import unittest

from helpers import CLAUDE

LIB = CLAUDE / "cli/agent_cli/eval_lib"
SENTINEL = "<promise>WIN</promise>"


def sh(body, env=None):
    """Run body after sourcing verdict.sh; env adds variables to a copy of os.environ."""
    return subprocess.run(["bash", "-c", f'source "{LIB / "verdict.sh"}"\n{body}'], text=True, capture_output=True,
                          env={**os.environ, **(env or {})})


def py(body, env=None):
    """The same shell-style body as Python: `fn "msg"` -> fn("msg"), `win` -> win(); env as for sh()."""
    body = "\n".join(re.sub(r'^(\w+) (".*")$', r"\1(\2)", ln) if ln != "win" else "win()" for ln in body.splitlines())
    code = f"import sys; sys.path.insert(0, {str(LIB)!r})\nfrom verdict import ok, fail, refuse, blocked, win\n{body}"
    return subprocess.run([sys.executable, "-c", code], text=True, capture_output=True,
                          env={**os.environ, **(env or {})})


class Verdict(unittest.TestCase):
    def test_fail_then_win_exits_1_without_the_sentinel(self):
        """A failed check can never be followed by a WIN: the loop reads the sentinel as the verdict."""
        for name, run in (("sh", sh), ("py", py)):
            # GIVEN a script calling fail then win
            # WHEN it runs
            r = run('fail "x broke"\nwin')
            # THEN exit 1 and no sentinel anywhere
            with self.subTest(lang=name):
                self.assertEqual(r.returncode, 1)
                self.assertNotIn(SENTINEL, r.stdout + r.stderr)
                self.assertIn("x broke", r.stdout)

    def test_ok_twice_then_win_prints_sentinel_last_and_exits_0(self):
        """Passing checks then win print the sentinel as the very last stdout line, exit 0."""
        for name, run in (("sh", sh), ("py", py)):
            # GIVEN two ok checks then win
            # WHEN it runs
            r = run('ok "a"\nok "b"\nwin')
            # THEN the sentinel is the last line
            with self.subTest(lang=name):
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(r.stdout.splitlines()[-1], SENTINEL)
                self.assertEqual(r.stdout.count(SENTINEL), 1)

    def test_refuse_and_blocked_exit_2_and_3(self):
        """refuse/blocked end the eval with refused (2) and blocked (3) from house-rules "exit-code table", no sentinel."""
        for name, run in (("sh", sh), ("py", py)):
            # GIVEN each call followed by win
            # WHEN run
            codes = [run(f'{fn} "why"\nwin') for fn in ("refuse", "blocked")]
            # THEN codes 2 and 3, no sentinel
            with self.subTest(lang=name):
                self.assertEqual([r.returncode for r in codes], [2, 3])
                self.assertFalse(any(SENTINEL in r.stdout for r in codes))

    def test_win_with_eval_only_set_exits_5_without_the_sentinel(self):
        """A partial run (`agent-cli eval run --only`) sets EVAL_ONLY; win must refuse, so it can never WIN."""
        for name, run in (("sh", sh), ("py", py)):
            # GIVEN EVAL_ONLY set and a passing check
            # WHEN win is called
            r = run('ok "a"\nwin', env={"EVAL_ONLY": "rows"})
            # THEN exit 5 (partial), no sentinel
            with self.subTest(lang=name):
                self.assertEqual(r.returncode, 5)
                self.assertNotIn(SENTINEL, r.stdout)
                self.assertIn("partial", r.stdout)

    def test_fail_inside_a_bash_subshell_still_blocks_win(self):
        """`( fail x )` or `$(fail x)` exits only the subshell; win must still refuse afterwards."""
        # GIVEN fail in a subshell, then win in the parent
        # WHEN run
        r = sh('( fail "sub" )\nout=$(fail "cap")\nwin')
        # THEN exit 1, no sentinel
        self.assertEqual(r.returncode, 1)
        self.assertNotIn(SENTINEL, r.stdout)


if __name__ == "__main__":
    unittest.main()
