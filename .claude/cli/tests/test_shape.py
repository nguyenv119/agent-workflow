"""Tests for agent_cli.shape: a Bash command reduced to its shape (names and flags kept, values masked)."""
import sys
import unittest

from helpers import CLAUDE

sys.path.insert(0, str(CLAUDE / "cli"))
from agent_cli.shape import shape  # noqa: E402

WRAP = ("wrapx", "wrapx raw")  # a made-up wrapper word, as a project's command-prefixes seam lists it

# (command, expected shape). Each row is one placeholder kind or one stripping rule.
CASES = [
    # command word and one subcommand kept
    ("git status", "git status"),
    ("ls", "ls"),
    ("gh pr view 12 --json state", "gh pr <arg> <n> --json <v>"),
    # seam wrapper words, cd chains, env and base prefixes stripped
    ("wrapx git status", "git status"),
    ("wrapx raw git log --oneline", "git log --oneline"),
    ("cd /x/y && git status", "git status"),
    ("cd /x; cd y; ls -la", "ls -la"),
    ("cd /x", "cd <path>"),
    ("FOO=bar BAZ=1 make build", "make build"),
    ("timeout 30 pytest -x", "pytest -x"),
    ("nohup python3 server.py &", "python3 <arg>"),
    ("wrapx raw DATABASE_URL=postgres://u:pw@h/db psql -c 'select 1' --csv", "psql -c <str> --csv"),
    ("X=$(git rev-parse HEAD) make", "make"),
    # quoted strings, backticks and heredocs
    ('psql -c "select count(*) from t" --csv', "psql -c <str> --csv"),
    ("echo 'hi there'", "echo <str>"),
    ("echo `date`", "echo <str>"),
    ("cat <<'EOF' > out.txt\nbody line\nEOF", "cat <heredoc> > <arg>"),
    ("python3 - <<EOF\nprint(1)\nEOF\necho done", "python3 - <heredoc>; echo done"),
    # numbers, ids, paths, urls
    ("sleep 30", "sleep <n>"),
    ("head -n 20 notes", "head -n <n> <arg>"),
    ("git show c324f28", "git show <id>"),
    ("bd show 5c685ddc-1a2b-4c3d-8e9f-001122334455", "bd show <id>"),
    ("cat /etc/hosts", "cat <path>"),
    ("ls ~", "ls <path>"),
    ("curl -s https://api.example.com/v2/x?y=1", "curl -s <url:api.example.com>"),
    ("psql postgres://u:pw@db.example.com:5432/x", "psql <url:db.example.com>"),
    ('curl -s -H "Authorization: Bearer abc" https://api.example.com/api/v2/projects/xyz',
     "curl -s -H <str> <url:api.example.com>"),
    # assignments and flag values
    ("export TOKEN=abc", "export TOKEN=<v>"),
    ("claude -p --model=haiku", "claude -p --model=<v>"),
    ("claude -p --model haiku", "claude -p --model <v>"),
    # separators: pipes, ||, newlines, subshells, redirections
    ("git log | head -n 5", "git log | head -n <n>"),
    ("test -f x || exit 1", "test -f <v> || exit <n>"),
    ("git add .\ngit commit -m 'msg'", "git add <path>; git commit -m <str>"),
    ("(cd x && make test)", "make test"),
    ("make 2>&1 | tee log.txt", "make 2>&1 | tee <arg>"),
    ("ls >/dev/null", "ls > <path>"),
    ("curl -s \\\n  https://x.example.com/a", "curl -s <url:x.example.com>"),
    # bare words, comments, repeats
    ("git checkout main", "git checkout <arg>"),
    ("ls README", "ls <arg>"),
    ("echo sk-live-ABCDEF0123456789", "echo <arg>"),
    ("ls # agent-cli: allow some-rule", "ls"),
    ("git add a.py b.py c.py", "git add <arg>"),
    ("git checkout -- file.py", "git checkout -- <arg>"),
]

UNPARSEABLE = ['echo "abc', "echo 'abc", "echo $(date", "echo `date", "", "   ", "# only a comment"]


class Shape(unittest.TestCase):
    def test_table(self):
        """Each command maps to its expected shape.

        The repetition spotter counts shapes across sessions, so each kind of value (string, number, id,
        path, url, env value, flag value) must collapse to one placeholder while names and flags stay;
        wrappers and cd chains must not split one chore into many shapes. A wrong mapping either leaks
        a value into the store or splits one chore across shapes so it is never counted as a repeat.
        """
        self.assertGreaterEqual(len(CASES), 30)
        for command, expected in CASES:
            with self.subTest(command=command):
                # GIVEN a command and the wrapper seam words
                # WHEN shaped
                got = shape(command, WRAP)
                # THEN the shape is the expected one
                self.assertEqual(got, expected)

    def test_unparseable_or_empty_is_none(self):
        """Unbalanced quotes or substitutions, and empty or comment-only input, give None.

        A half-parsed command could put quoted text in a word position, where it would be stored as a
        name; None means "record no shape" instead of a wrong or leaky one.
        """
        for command in UNPARSEABLE:
            with self.subTest(command=command):
                # GIVEN input that cannot be split into words
                # WHEN shaped
                # THEN no shape
                self.assertIsNone(shape(command, WRAP))

    def test_commands_differing_only_in_values_share_one_shape(self):
        """Two runs of one chore with different ids, paths, strings and wrappers give the same shape.

        This is what makes a chore countable: if the shape kept any value, every run would be its own
        shape and none would ever reach the 3-session threshold.
        """
        # GIVEN one chore typed twice with different values, once through the wrapper
        a = "wrapx gh pr view 123 --repo a/b --json state && curl -s https://h.example.com/x/1 -H 'k: v'"
        b = "cd /else && gh pr view 4567 --repo c/d --json state && curl -s https://h.example.com/y?z -H \"q\""
        # WHEN both are shaped
        # THEN the shapes are equal
        self.assertEqual(shape(a, WRAP), shape(b, WRAP))
        self.assertEqual(shape(a, WRAP), "gh pr <arg> <n> --repo <path> --json <v> && curl -s <url:h.example.com> -H <str>")

    def test_wrapper_is_kept_without_the_seam(self):
        """With no seam words, a wrapper is an ordinary command word.

        The seam is the only place a project names its wrappers; shape must not hard-code any, or a
        generic install would strip words that are real commands elsewhere.
        """
        # GIVEN no prefixes
        # WHEN a wrapped command is shaped
        # THEN the wrapper stays as the command word
        self.assertEqual(shape("wrapx git status", ()), "wrapx git <arg>")

    def test_shape_never_contains_a_value_typed_in_any_position(self):
        """A planted value in every value position never appears in the shape.

        The store must hold no secret; this is the in-memory half of the planted-secret test in
        test_record (which greps the files on disk).
        """
        # GIVEN a secret in each position
        s = "sk-live-PLANTED0123456789"
        commands = [f"S={s} make", f"curl -H 'Authorization: {s}' https://h.example.com", f"tool {s}",
                    f"curl https://h.example.com/x?key={s}", f"curl https://h.example.com/{s}/x",
                    f"deploy --token {s}", f"deploy --token={s}", f"cat <<EOF\n{s}\nEOF", f"export K={s}",
                    f"{s} --x", f"tool -{s}", f"curl https://{s}@h.example.com"]
        for command in commands:
            with self.subTest(command=command):
                # WHEN shaped
                got = shape(command, WRAP) or ""
                # THEN no 6-char piece of the secret survives
                self.assertFalse([s[i:i + 6] for i in range(len(s) - 5) if s[i:i + 6] in got], got)


if __name__ == "__main__":
    unittest.main()
