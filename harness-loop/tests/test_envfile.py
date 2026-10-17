"""Tests for harness-loop/exam/envfile.py: the plan token from HARNESS_EXAM_ENV_FILE, one key only."""
import os
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "exam"))
import envfile  # noqa: E402

KEY = "CLAUDE_CODE_OAUTH_TOKEN"
DUMMY = "dummy-plan-value"


class WithToken(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.file = Path(self.tmp.name) / "exam.env"

    def env(self, text, **extra):
        self.file.write_text(text)
        return {envfile.ENV_FILE: str(self.file), **extra}

    def test_reads_only_the_token_line_in_its_shell_forms(self):
        """
        What: `KEY=v`, `export KEY=v`, quoted values and a trailing comment all give v; other keys are not copied.
        Why: the file is a shell env file holding other secrets. Breaks: AWS keys reach the exam, or a quote does.
        """
        for line in (f"{KEY}={DUMMY}", f"export {KEY}={DUMMY}", f"{KEY}='{DUMMY}'", f'{KEY}="{DUMMY}"',
                     f"  {KEY} = {DUMMY}  # plan token"):
            with self.subTest(line=line):
                # GIVEN a file with the line among other keys
                env = self.env(f"AWS_SECRET_ACCESS_KEY=x\n{line}\nANTHROPIC_API_KEY=y\n")
                # WHEN loaded / THEN only the token is added, the input is not mutated
                out = envfile.with_token(env)
                self.assertEqual(out[KEY], DUMMY)
                self.assertNotIn("AWS_SECRET_ACCESS_KEY", out)
                self.assertNotIn(KEY, env)

    def test_a_set_token_wins_and_an_empty_one_does_not(self):
        """
        What: a non-empty token in the env skips the file; an empty one reads it.
        Why: the brief's rule (shell wins). Breaks: a stale file overrides a fresh login.
        """
        self.assertEqual(envfile.with_token(self.env(f"{KEY}={DUMMY}\n", **{KEY: "shell"}))[KEY], "shell")
        self.assertEqual(envfile.with_token(self.env(f"{KEY}={DUMMY}\n", **{KEY: ""}))[KEY], DUMMY)

    def test_no_env_file_and_no_token_line_change_nothing(self):
        """What: no HARNESS_EXAM_ENV_FILE, or a file without the key, returns the env as it was."""
        self.assertEqual(envfile.with_token({"A": "1"}), {"A": "1"})
        self.assertNotIn(KEY, envfile.with_token(self.env("OTHER=1\n")))

    def test_missing_file_raises_naming_the_path_only(self):
        """
        What: a named file that does not exist raises RuntimeError naming the path.
        Why: callers turn it into a refusal before any run. Breaks: a silent fall-through to no token.
        """
        missing = Path(self.tmp.name) / "nope.env"
        with self.assertRaises(RuntimeError) as cm:
            envfile.with_token({envfile.ENV_FILE: str(missing)})
        self.assertIn(str(missing), str(cm.exception))

    def test_tilde_is_expanded(self):
        """What: `~/x.env` resolves under HOME (the project-rules line is written that way)."""
        self.file.write_text(f"{KEY}={DUMMY}\n")
        with unittest.mock.patch.dict(os.environ, {"HOME": self.tmp.name}):
            self.assertEqual(envfile.with_token({envfile.ENV_FILE: "~/exam.env"})[KEY], DUMMY)


if __name__ == "__main__":
    unittest.main()
