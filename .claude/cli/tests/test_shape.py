"""Tests for agent_cli.shape: a Bash command reduced to its shape (names and flags kept, values masked)."""
import sys
import unittest

from helpers import CLAUDE

sys.path.insert(0, str(CLAUDE / "cli"))
from agent_cli import record  # noqa: E402
from agent_cli.shape import shape, steps, steps_of_shape  # noqa: E402

WRAP = ("wrapx", "wrapx raw")  # a made-up wrapper word, as a project's command-prefixes seam lists it
TOOLS = ()  # no subcommand-tools seam words, so the developer's own seam never decides a row

# (command, expected shape). Each row is one placeholder kind or one stripping rule.
CASES = [
    # command word and one subcommand kept
    ("git status", "git status"),
    ("ls", "ls"),
    ("gh pr view 12 --json state", "gh pr view <n> --json <v>"),
    # seam wrapper words, cd chains, env and base prefixes stripped
    ("wrapx git status", "git status"),
    ("wrapx raw git log --oneline", "git log --oneline"),
    ("cd /x/y && git status", "git status"),
    ("cd /x; cd y; ls -la", "ls -la"),
    ("cd /x", "cd <path>"),
    ("FOO=bar BAZ=1 make build", "make <arg>"),
    ("timeout 30 pytest -x", "pytest -x"),
    ("nohup python3 server.py &", "python3 server.py"),
    ("wrapx raw DATABASE_URL=postgres://u:pw@h/db psql -c 'select 1' --csv", "psql -c <str> --csv"),
    ("X=$(git rev-parse HEAD) make", "make"),
    ("sleep $((60*5))", "sleep <str>"),
    # quoted strings, backticks and heredocs
    ('psql -c "select count(*) from t" --csv', "psql -c <str> --csv"),
    ("echo 'hi there'", "echo <str>"),
    ("echo `date`", "echo <str>"),
    ("cat <<'EOF' > out.txt\nbody line\nEOF", "cat <heredoc> > <arg>"),
    ("python3 - <<EOF\nprint(1)\nEOF\necho done", "python3 - <heredoc>; echo <arg>"),
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
    ("(cd x && git status)", "git status"),
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
    # a value never sits in a name slot
    ("git c324f28ab", "git <id>"),
    ("curl https://sk-live-PLANTED0123456789.example.com", "curl <url>"),
    ("echo correcthorse | tool login --password-stdin", "echo <arg> | tool <arg> --password-stdin"),
    ("ssh myprivatehost", "ssh <arg>"),
    ("command -v hunterpw", "-v <v>"),
    ("env -u AWS_PROFILE -i gh pr view 12", "gh pr view <n>"),  # env's own options go with it
    ("mysql -uroot -pS3cr3tPw db", "mysql -u<v> -p<v> <v>"),
    ("echo dGhpc2lzYXNlY3JldA== | base64 -d", "echo <arg> | base64 -d"),
    # commands run by path keep their name; known multi-command tools keep their subcommand
    ("/opt/x/.claude/bin/agent-cli wt list --json", "<path>/agent-cli wt list --json"),
    ("./scripts/deploy.sh --dry-run", "<path>/deploy.sh --dry-run"),
    ("demo-cli run job", "demo-cli run <arg>"),
    # an interpreter keeps its script's name, so `python3 a.py` and `python3 b.py` are two chores
    ("python3 /x/harness-loop/loop.py leak --diff origin/main", "python3 <path>/loop.py <arg> --diff origin/<arg>"),
    ("python3 -u tools/check.py 3", "python3 -u <path>/check.py <n>"),
    ("bash ./run_eval.sh && node build.mjs", "bash <path>/run_eval.sh && node build.mjs"),
    ("python3 -c 'print(1)' x.py", "python3 -c <str> <arg>"),
    ("cat notes.py", "cat <arg>"),
    ("python3 /x/a.py /y/b.py", "python3 <path>/a.py <path>"),
    ("python3 /x/c324f28ab.py", "python3 <path>"),
    # single-dash long flags lose their names (privacy first: -pS3cret looks the same)
    ("find . -maxdepth 2 -name x -print", "find <path> -m<v> <n> -name <v> -p<v>"),
    # env files and `.`/`source` keep their names, so loading env is one chore however it is spelled
    (". .env", ". .env"),
    ("source ~/proj/.env.local", "source <path>/.env.local"),
    ("grep KEY prod.env", "grep <arg> prod.env"),
    ("cat .env.test", "cat .env.test"),
    ("set -a; set +a", "set -a; set +a"),
    # git global options are skipped before the subcommand slot
    ("git -C ../wt status", "git -C <path> status"),
    ("git --no-pager log -1", "git --no-pager log <n>"),
    ("git -c core.pager=cat --git-dir=/x/.git diff", "git -c <v> --git-dir=<v> diff"),
    ("git --work-tree /x --git-dir /x/.git status", "git --work-tree <path> --git-dir <path> status"),
    # two-level CLIs keep a third word; package runners keep script names with a colon
    ("gh pr list --state open", "gh pr list --state <v>"),
    ("gh issue list --state open", "gh issue list --state <v>"),
    ("gh release create v1.2", "gh release create <arg>"),
    ("gh workflow run ci.yml", "gh workflow run <arg>"),
    ("git remote add up https://g.example.test/a", "git remote add <arg> <url:g.example.test>"),
    ("git submodule update --init", "git submodule update --init"),
    ("gh run view 99 --log", "gh run view <n> --log"),
    ("git worktree add -b feat ../wt2", "git worktree add -b <v> <path>"),
    ("git stash pop", "git stash pop"),
    ("agent-cli wt list", "agent-cli wt list"),
    ("git log main", "git log <arg>"),
    ("pnpm db:query 'select 1'", "pnpm db:query <str>"),  # a built-in runner, e.g. pnpm with no seam
    ("yarn db:branch", "yarn db:branch"),
    ("npm run test:unit", "npm run test:unit"),
    ("yarn run test:unit", "yarn run test:unit"),
    ("yarn test:unit", "yarn test:unit"),
    # pnpm global options are skipped like git's; exec and dlx keep the tool they run
    ("pnpm --filter @x/web typecheck", "pnpm --filter <path> typecheck"),
    ("pnpm --filter=web typecheck", "pnpm --filter=<v> typecheck"),
    ("pnpm -F web lint", "pnpm -F <v> lint"),
    ("pnpm -r lint", "pnpm -r lint"),
    ("pnpm -w -s build", "pnpm -w -s build"),
    ("pnpm --recursive --silent --workspace-root test", "pnpm --recursive --silent --workspace-root test"),
    ("pnpm -C apps/web run test:unit", "pnpm -C <path> run test:unit"),
    ("pnpm --dir apps/web typecheck", "pnpm --dir <path> typecheck"),
    ("pnpm --dir=apps/web typecheck", "pnpm --dir=<v> typecheck"),
    ("pnpm --filter web db:query 'select 1'", "pnpm --filter <v> db:query <str>"),
    ("pnpm --filter secretpkg typecheck", "pnpm --filter <v> typecheck"),
    ("pnpm exec tsc --noEmit", "pnpm exec tsc --noEmit"),
    ("pnpm --filter web exec tsc", "pnpm --filter <v> exec tsc"),
    ("pnpm dlx prisma generate", "pnpm dlx prisma <arg>"),
    ("pnpm exec c324f28ab", "pnpm exec <id>"),
    ("pnpm exec tool2024x", "pnpm exec <arg>"),
    ("npm --filter web typecheck", "npm --filter <v> <arg>"),  # only pnpm's options are pnpm's
    # gh api keeps the endpoint's GitHub resource words; owner, repo, numbers, ids and queries are masked
    ("gh api repos/acme-co/private-repo/pulls", "gh api repos/<arg>/<arg>/pulls"),
    ("gh api repos/o/r/pulls/12/comments", "gh api repos/<arg>/<arg>/pulls/<n>/comments"),
    ("gh api /repos/o/r/actions/runs/99/jobs --jq '.jobs[]'", "gh api repos/<arg>/<arg>/actions/runs/<n>/jobs --jq <str>"),
    ("gh api repos/o/r/commits/c324f28ab/check-runs", "gh api repos/<arg>/<arg>/commits/<id>/check-runs"),
    ("gh api repos/o/r/pulls?per_page=100 --paginate", "gh api repos/<arg>/<arg>/pulls?<v> --paginate"),
    ("gh api --paginate repos/o/r/issues", "gh api --paginate repos/<arg>/<arg>/issues"),
    ("gh api repos/{owner}/{repo}/pulls", "gh api repos/<arg>/<arg>/pulls"),
    ("gh api repos/o/actions/pulls", "gh api repos/<arg>/<arg>/pulls"),  # a repo named like a resource word
    ("gh api repos/o/r/privatething/x", "gh api repos/<arg>/<arg>/<arg>/<arg>"),
    ("gh api users/someone/repos", "gh api users/<arg>/repos"),
    ("gh api graphql -f query='{ viewer { login } }'", "gh api graphql -f query=<v>"),
    ("gh api user", "gh api user"),
    ("gh api octocat", "gh api <arg>"),
    ("gh api / --jq .x", "gh api <path> --jq <v>"),
    ("gh api 'repos/o/r/pulls?per_page=100'", "gh api <str>"),  # a quoted endpoint is quoted text
    ("gh api https://api.example.test/repos/o/r/pulls", "gh api <url:api.example.test>"),
    ("gh api repos/o/r/check-runs/99/annotations", "gh api repos/<arg>/<arg>/check-runs/<n>/annotations"),
    ("git a:b", "git <arg>"),
    ("yarn db:c324f28ab", "yarn <arg>"),
    ("yarn run2024:x", "yarn <arg>"),
    # remote branch refs keep their remote, so on-main checks are not read as file paths
    ("git log origin/main..", "git log origin/<arg>"),
    ("git show origin/main:a.ts", "git show origin/<arg>"),
    ("git rebase upstream/dev", "git rebase upstream/<arg>"),
    ("git log --oneline 1757acaa3..origin/main", "git log --oneline <id>..origin/<arg>"),
    ("git diff HEAD...upstream/dev", "git diff <arg>...upstream/<arg>"),
    ("git log ..origin/main", "git log ..origin/<arg>"),
    # an assignment of a path is an assignment, not a command run by path
    ("F=/tmp/x", "F=<v>"),
    # `[` and `[[` keep their name, so a test reads as a test and not as a masked command
    ('[ -n "$x" ]', "[ -n <str> <arg>"),
    ('[ "$a" = "$b" ]', "[ <str> <arg> <str> <arg>"),
    ("[[ -f a.txt ]]", "[[ -f <v> <arg>"),
]

UNPARSEABLE = ["cat <<$D\nsecret line\n$D", 'echo "abc', "echo 'abc", "echo $(date", "echo `date", "", "   ", "# only a comment"]


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
                got = shape(command, WRAP, TOOLS)
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
        self.assertEqual(shape(a, WRAP), "gh pr view <n> --repo <path> --json <v> && curl -s <url:h.example.com> -H <str>")

    def test_wrapper_is_kept_without_the_seam(self):
        """With no seam words, a wrapper is an ordinary command word (and, not being a known
        multi-command tool, its next words are values).

        The seam is the only place a project names its wrappers; shape must not hard-code any, or a
        generic install would strip words that are real commands elsewhere.
        """
        # GIVEN no prefixes
        # WHEN a wrapped command is shaped
        # THEN the wrapper stays as the command word
        self.assertEqual(shape("wrapx git status", ()), "wrapx <arg>")

    def test_subcommand_tools_seam_adds_tools(self):
        """A tool named in the subcommand-tools seam keeps its second word; an unnamed one does not.

        A project's own multi-command tools (a package runner, a deploy CLI) would otherwise collapse
        every subcommand into one shape, so their chores could never be told apart.
        """
        # GIVEN a made-up tool, once named in the seam list and once not
        # WHEN shaped
        # THEN only the named one keeps its subcommand
        self.assertEqual(shape("tasker run build", WRAP, ["tasker"]), "tasker run <arg>")
        self.assertEqual(shape("tasker run build", WRAP, []), "tasker <arg>")

    def test_shape_never_contains_a_value_typed_in_any_position(self):
        """A planted value in every value position never appears in the shape.

        The store must hold no secret; this is the in-memory half of the planted-secret test in
        test_record (which greps the files on disk).
        """
        # GIVEN a secret in each position
        s, w = "sk-live-PLANTED0123456789", "correcthorsebatterystaple"  # w: no digits, all lowercase
        commands = [f"echo {w}", f"printf {w} | gpg --batch", f"tool -p{w}", f"command -v {w}", f"ssh {w}",
                    f"echo {w}== | base64 -d", f"cat <<$D\n{w}\n$D", f"curl https://{s}.example.com",
                    f"sudo -S {w}", f"env -u {w} make",
                    f"S={s} make", f"curl -H 'Authorization: {s}' https://h.example.com", f"tool {s}",
                    f"curl https://h.example.com/x?key={s}", f"curl https://h.example.com/{s}/x",
                    f"deploy --token {s}", f"deploy --token={s}", f"cat <<EOF\n{s}\nEOF", f"export K={s}",
                    f"{s} --x", f"tool -{s}", f"curl https://{s}@h.example.com",
                    # w is 25 chars, one over the 24-char subcommand limit, so it is never kept as one
                    f"gh pr {w}", f"agent-cli wt {w}", f"yarn db:{w}", f"git -C {w} status",
                    f"git log origin/{s}", f"git log {s}..origin/{s}",
                    f"pnpm --filter {w} typecheck", f"pnpm --filter=@{w}/x lint", f"pnpm -C {w} exec {w}",
                    f"pnpm dlx {w}", f"gh api repos/{w}/{w}/pulls", f"gh api {w}", f"gh api /orgs/{w}/members",
                    f"gh api repos/o/r/pulls?{w}=1", f"gh api repos/o/r/{w}/{s}", f"gh api '{w}/{s}'", f"echo $(cat {w})", f"echo \"$(tool {s})\"",
                    f"echo $(({w}+1))", f"source .env.prod && deploy --token {s} -m '{w}'"]
        for command in commands:
            with self.subTest(command=command):
                # WHEN shaped, whole and as steps (steps also shape each $( ) body)
                got = ((shape(command, WRAP, TOOLS) or "") + " " + " ".join(steps(command, WRAP, TOOLS))).lower()
                # THEN no 6-char piece of either secret survives
                self.assertFalse([x[i:i + 6] for x in (s.lower(), w) for i in range(len(x) - 5)
                                  if x[i:i + 6] in got], got)
        # AND an env-file basename is kept on purpose (a name, never a value) while the values beside it go
        self.assertEqual(shape(f"source .env.prod && deploy --token {s} -m '{w}'", WRAP, TOOLS),
                         "source .env.prod && deploy --token <v> -m <str>")


# (command, expected steps). Each row is one splitting or dropping rule.
STEP_CASES = [
    # the bead's example: a chained env-load and DB query split into its chores
    ("cd x && set -a; . .env; set +a; yarn db:query 'select 1' 2>&1 | tail -5",
     ["set -a", ". .env", "set +a", "yarn db:query <str> | tail <n>"]),
    # a pipeline is one recipe; && || & ; and newlines split
    ("grep x f | sort | uniq -c", ["grep <arg> | sort | uniq -c"]),
    ("make && make test || echo bad & wait; ls\npwd", ["make", "make <arg>", "echo <arg>", "wait", "ls", "pwd"]),
    # quoted separators, 2>&1 and \-newline never split a step
    ("echo 'a; b && c' 2>&1", ["echo <str>"]),
    ("curl -s \\\n  https://x.example.com/a && ls", ["curl -s <url:x.example.com>", "ls"]),
    # redirections are dropped with their targets
    ("make > out.txt 2>&1; cat f >> /x/log; sort < in.txt", ["make", "cat <arg>", "sort"]),
    # keywords and loop headers are dropped; the loop body stays
    ("for f in a b; do echo $f; continue; done", ["echo <arg>"]),
    ("if test -f x; then echo hi; fi", ["test -f <v>", "echo <arg>"]),
    ("case $x in a) break;; esac; ls", ["ls"]),
    ("select x in a b; do echo $x; done", ["echo <arg>"]),
    ("{ echo a; }", ["echo <arg>"]),
    ("while read l; do echo $l; done < f", ["read <arg>", "echo <arg>"]),
    # assignment-only steps are dropped, a path assignment included
    ("F=/tmp/x; X=1 Y=2; ls", ["ls"]),
    ("for f in a; do p=$(cat $f); done", ["cat <arg>"]),
    ("if test -f x; then s=1; fi", ["test -f <v>"]),
    ("git log --oneline 1757acaa3..origin/main", ["git log --oneline <id>..origin/<arg>"]),
    # <path> folds into <arg> (whole words only), and repeats are kept once in first order
    ("cat /x/y; cat a.txt /x/b; echo 'a' > f 'b'", ["cat <arg>", "echo <str>"]),
    ("git diff origin/main x", ["git diff origin/<arg> <arg>"]),
    ("./scripts/deploy.sh; git status; git status", ["<path>/deploy.sh", "git status"]),
    ("git -C ../wt worktree add -b feat ../wt2", ["git -C <arg> worktree add -b <v> <arg>"]),
    ("gh pr view 12 --json state", ["gh pr view <n> --json <v>"]),
    ("gh pr list --state open", ["gh pr list --state <v>"]),
    ("pnpm --filter web typecheck && pnpm -r lint", ["pnpm --filter <v> typecheck", "pnpm -r lint"]),
    ("gh api repos/o/r/pulls/1 | jq .x", ["gh api repos/<arg>/<arg>/pulls/<n> | jq <arg>"]),
    # each $( ) or backtick body adds its own steps, inside double quotes too, one level deep
    ("TOKEN=$(grep KEY .env | cut -d= -f2)", ["grep <arg> .env | cut -d=<v> -f<v>"]),
    ('echo "v=$(git rev-parse HEAD)"', ["echo <str>", "git rev-parse <arg>"]),
    ("echo `date`; date", ["echo <str>", "date"]),
    ("echo $(a $(b x))", ["echo <str>", "a <str>"]),
    ('echo "$(ls)" `ls`', ["echo <str>", "ls"]),
    # $(( )) is arithmetic, not a command: masked, and its body adds no step
    ("sleep $((60*5))", ["sleep <str>"]),
    ("n=$((n+1)); echo $n", ["echo <arg>"]),
    ('echo "elapsed $(( end - start ))s"', ["echo <str>"]),
    ('echo "$(( a - b ))"', ["echo <str>"]),
]


class Steps(unittest.TestCase):
    def test_table(self):
        """Each command maps to its expected list of step shapes.

        The repeat report counts steps, not whole commands, because most hand-done chores sit inside
        chains: `. .env` as step 3 of a chain must count the same as `. .env` alone. A pipeline split
        would make `sort` and `uniq -c` top every list; a missed split spreads one chore over thousands
        of whole-command shapes so it never reaches the repeat threshold.
        """
        for command, expected in STEP_CASES:
            with self.subTest(command=command):
                # GIVEN a command and the wrapper seam words
                # WHEN split into steps
                got = steps(command, WRAP, TOOLS)
                # THEN the steps are the expected ones, in order
                self.assertEqual(got, expected)

    def test_unparseable_or_empty_gives_no_steps(self):
        """Input shape() rejects gives an empty list, never a partial one.

        A half-parsed command could put quoted text in a word position; the store must get no step
        rather than a wrong or leaky one.
        """
        for command in UNPARSEABLE + ['echo "$(date"', "ls $(date) 'x"]:
            with self.subTest(command=command):
                # GIVEN input shape() cannot parse
                # WHEN split into steps
                # THEN no steps
                self.assertEqual(steps(command, WRAP), [])

    def test_long_substitution_body_is_skipped(self):
        """A $( ) body over 2,000 characters adds no steps; the outer command still gives its own.

        Bodies that long are inline scripts, not chores; shaping them costs time on every recorded
        command and adds one-off steps that are never repeats.
        """
        # GIVEN a substitution whose body is just over the cap, and one just under
        long_body = "echo " + "x" * 1996
        short_body = "echo " + "x" * 1995
        # WHEN split into steps
        # THEN only the short body adds a step
        self.assertEqual(steps(f"tool $({long_body})", WRAP), ["tool <str>"])
        self.assertEqual(steps(f"tool $({short_body})", WRAP), ["tool <str>", "echo <arg>"])

    def test_steps_of_stored_shape_string(self):
        """steps_of_shape splits an already-stored shape string the same way steps() splits a command.

        Old store rows hold only the whole shape, never the raw command; readers must be able to turn
        them into steps, or a week of history would be counted differently from new rows.
        """
        # GIVEN a shape string as the recorder stored it before steps existed
        stored = "set -a; <path> <arg>; for <arg>; F=<v>; cat <path> 2>&1 > <path> | tail <n>; done"
        # WHEN split
        got = steps_of_shape(stored)
        # THEN keywords, assignments and redirects are dropped and <path> folds into <arg>
        self.assertEqual(got, ["set -a", "<arg>", "cat <arg> | tail <n>"])

    def test_step_is_cut_to_the_store_limit(self):
        """Each step is cut to record.MAX_SHAPE, the limit the recorder applies to stored shapes.

        A step longer than a stored shape could never match the same chore read back from an old row.
        """
        # GIVEN a shape longer than the store limit
        long = "tool " + "x" * (2 * record.MAX_SHAPE)
        # WHEN split
        got = steps_of_shape(long)
        # THEN its one step is cut to the limit
        self.assertEqual(got, [long[:record.MAX_SHAPE]])

    def test_path_is_not_folded_in_the_whole_shape(self):
        """shape() keeps <path>; only steps fold it into <arg>.

        The whole shape is what the hook's own rules and old rows compare against; folding there would
        silently change every stored key.
        """
        # GIVEN a command with a path argument
        # WHEN shaped whole and as steps
        # THEN only the steps fold the path
        self.assertEqual(shape("cat /x/y", WRAP), "cat <path>")
        self.assertEqual(steps("cat /x/y", WRAP), ["cat <arg>"])


if __name__ == "__main__":
    unittest.main()
