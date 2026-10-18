"""Tests for agent_cli.effects: the hosts, env var names and .env files a Bash command touches, never values."""
import sys
import time
import unittest

from helpers import CLAUDE

sys.path.insert(0, str(CLAUDE / "cli"))
from agent_cli.effects import effects  # noqa: E402

WRAP = ("wrapx", "wrapx raw")  # a made-up wrapper word, as a project's command-prefixes seam lists it

# The commit form Claude Code writes: the message is a heredoc inside "$( )".
COMMIT = ("git commit -m \"$(cat <<'EOF'\nfix: thing\n\nhttps://claude.com/claude-code\nEOF\n)\"")


def fx(command):
    return effects(command, WRAP)


class BeadExamples(unittest.TestCase):
    def test_env_load_and_api_call_give_names_file_and_host(self):
        """The task's first example: an env load plus an authorized curl gives the two variable names, the
        env file and the API host, sorted.

        This is the chore the weekly count exists to find (one database-API call typed many ways); missing any of
        the four signals splits or hides it.
        """
        # GIVEN the env-load-then-curl chain
        cmd = ('set -a; . ../.env; set +a; curl -s -H "Authorization: Bearer $DBAPI_KEY" '
               "https://console.dbapi.example.test/api/v2/projects/$DBAPI_PROJECT_ID/branches")
        # WHEN its effects are read
        # THEN names, file and host, sorted
        self.assertEqual(fx(cmd), ["env:DBAPI_KEY", "env:DBAPI_PROJECT_ID", "envfile:.env",
                                   "host:console.dbapi.example.test"])

    def test_grep_pattern_is_ignored_but_its_file_counts(self):
        """The task's second example: `grep -n NAME file.env.example` gives the env file only.

        A review grep for a variable name does not read the variable; counting it would fill the report with
        reads of the code, not of the environment.
        """
        # WHEN a grep for a variable name in an env example file is read
        # THEN only the file
        self.assertEqual(fx("grep -n DBAPI_KEY .claude/.env.example"), ["envfile:.env.example"])

    def test_commit_message_url_gives_nothing(self):
        """The task's third example: a commit whose message holds a URL gives no effect.

        On the dev week `host:claude.com` came from commit and PR bodies in 40 sessions; it is text, not a call.
        """
        # WHEN a commit with a URL in its -m text is read
        # THEN nothing
        self.assertEqual(fx('git commit -m "fix: x https://claude.com/claude-code"'), [])


class Signals(unittest.TestCase):
    def test_host_drops_userinfo_and_port_and_is_lowercased(self):
        """A URL's host is stored without its user, password or port, in lowercase.

        The userinfo of a connection URL is where its password sits; the port and case would split one host
        into several rows.
        """
        # WHEN a connection URL with user, password and port is read
        got = fx("psql postgres://admin:hunter2pw@DB.Example.Test:5432/app")
        # THEN the bare lowercase host
        self.assertEqual(got, ["host:db.example.test"])

    def test_host_with_4_digit_run_or_odd_chars_is_skipped(self):
        """A host holding a 4+ digit run (an id or a generated name) or chars a host cannot hold is not stored.

        Such hosts are values (a preview deploy id, a tunnel id), not a place a chore keeps going.
        """
        # WHEN URLs with a digit-laden host and with a variable as host are read
        # THEN neither is stored
        self.assertEqual(fx("curl https://pr-12345.example.test/x https://$host/y"), [])

    def test_quoted_url_host_is_cut_at_the_quote(self):
        """A URL inside quotes gives its host without the closing quote.

        Most real calls quote their URL; a host with a quote glued on would fail the host check and the
        call would vanish from the count.
        """
        # WHEN a quoted URL is read
        # THEN its host
        self.assertEqual(fx("curl -s 'https://api.example.test'"), ["host:api.example.test"])

    def test_each_env_read_form_counts_in_both_quote_styles(self):
        """$NAME, ${NAME...}, os.environ[...], os.environ.get, os.getenv, process.env.NAME and ENV[...] each
        give env:NAME, with single or double quotes around the name.

        Agents write the same read many ways; a form left out would hide a chore behind its spelling.
        """
        cases = [("echo ok; tool $ALPHA_KEY", "ALPHA_KEY"), ('tool "${BETA_KEY:-x}"', "BETA_KEY"),
                 ("python3 -c \"import os; os.environ['GAMMA_KEY']\"", "GAMMA_KEY"),
                 ("python3 -c 'import os; os.environ[\"DELTA_KEY\"]'", "DELTA_KEY"),
                 ("python3 -c \"import os; os.environ.get('EPS_KEY')\"", "EPS_KEY"),
                 ("python3 -c 'import os; os.getenv(\"ZETA_KEY\")'", "ZETA_KEY"),
                 ("node -e 'console.log(process.env.ETA_KEY)'", "ETA_KEY"),
                 ("ruby -e 'puts ENV[\"THETA_KEY\"]'", "THETA_KEY")]
        for cmd, name in cases:
            with self.subTest(cmd=cmd):
                # WHEN the command is read
                # THEN the name
                self.assertEqual(fx(cmd), [f"env:{name}"])

    def test_names_outside_the_name_rule_or_on_the_stop_list_are_dropped(self):
        """Lowercase or short names, names with a 4+ digit run and shell basics (HOME, PATH, ...) give nothing.

        Every command touches HOME or PATH; listing them would put noise at the top of every report.
        """
        # WHEN a command reads only such names
        cmd = 'tool "$HOME" $PATH $PWD $USER $TMPDIR $IFS $lower $AB $KEY12345 "${BASH_SOURCE[0]}"'
        # THEN nothing
        self.assertEqual(fx(cmd), [])

    def test_env_file_forms_count_by_basename(self):
        """.env, .env.<x> and <x>.env count by basename, as a bare word, a quoted string, a flag value or in code.

        The env file is the third effect a secret chore leaves; its path differs per worktree, so only the
        basename joins the runs.
        """
        # WHEN commands load env files in four ways
        # THEN each basename once; process.env (code, not a file) is not one
        cmd = ("source ../../.env.local; docker run --env-file=prod.env img; "
               "python3 -c \"from dotenv import load_dotenv; load_dotenv('.env')\"; "
               "node -e 'Object.keys(process.env)'")
        self.assertEqual(fx(cmd), ["envfile:.env", "envfile:.env.local", "envfile:prod.env"])

    def test_at_most_32_sorted_effects(self):
        """A command touching 40 hosts gives the first 32 effects in sorted order.

        Rows are appended with one write by many sessions; an unbounded list would make long lines.
        """
        # WHEN a command calls 40 hosts
        cmd = " && ".join(f"curl https://h{chr(97 + i // 26)}{chr(97 + i % 26)}.example.test" for i in range(40))
        got = fx(cmd)
        # THEN 32, sorted
        self.assertEqual(len(got), 32)
        self.assertEqual(got, sorted(got))
        self.assertEqual(got[0], "host:haa.example.test")


class InlineCode(unittest.TestCase):
    def test_python_heredoc_env_read_counts(self):
        """Code fed to an interpreter as a heredoc counts: its env reads and URLs are what the chore does.

        Dev-DB queries run as `python3 - <<'EOF'`; skipping heredoc bodies would hide every one of them.
        """
        # GIVEN an inline python query
        cmd = "python3 - <<'EOF'\nimport os, psycopg\nc = psycopg.connect(os.environ['DATABASE_URL'])\nEOF"
        # WHEN read
        # THEN its env read
        self.assertEqual(fx(cmd), ["env:DATABASE_URL"])

    def test_node_e_and_psql_with_flags_count(self):
        """`node -e` code and `psql "$DATABASE_URL" -c ...` give the variable, whatever the flags.

        The same query is typed with varying flags; the effect is what makes those runs one chore.
        """
        for cmd in ("node -e 'const u = process.env.DATABASE_URL; console.log(u)'",
                    'psql "$DATABASE_URL" -At -c "select count(*) from t"',
                    'wrapx raw psql "$DATABASE_URL" -c "select 1"'):
            with self.subTest(cmd=cmd):
                self.assertEqual(fx(cmd), ["env:DATABASE_URL"])

    def test_heredoc_written_to_a_file_is_skipped(self):
        """A heredoc redirected to a file (`cat > f <<EOF`, `cat <<EOF > f`, `tee f <<EOF`) gives nothing.

        Writing a doc or a script is not running it; the doc's URLs and variable names are text.
        """
        body = "\nSee https://docs.example.test and $API_TOKEN\nEOF"
        for cmd in ("cat > notes.md <<'EOF'" + body, "cat <<EOF > notes.md" + body, "tee notes.md <<EOF" + body):
            with self.subTest(cmd=cmd.split("\n")[0]):
                self.assertEqual(fx(cmd), [])

    def test_dollar_name_in_a_quoted_heredoc_fed_to_python_is_text(self):
        """`$NAME` in a quoted heredoc (`<<'EOF'`) fed to python is text, not a read; os.environ there still counts.

        No shell expands a quoted heredoc and python never expands `$`; on the dev week `env:JSON` reached 18
        sessions only from python code that edits shell scripts.
        """
        # GIVEN python editing a shell script that mentions $JSON_OUT, and reading API_TOKEN itself
        cmd = "python3 - <<'EOF'\nimport os\ns = s.replace('$JSON_OUT', 'x')\nos.environ['API_TOKEN']\nEOF"
        # WHEN read
        # THEN only the python read
        self.assertEqual(fx(cmd), ["env:API_TOKEN"])

    def test_dollar_name_counts_in_unquoted_heredocs_and_shell_heredocs(self):
        """`$NAME` counts in an unquoted heredoc (the shell expands it) and in a quoted one fed to bash.

        Both are real reads: the shell substitutes the value before python runs, or bash runs the body.
        """
        for cmd in ("python3 - <<EOF\nprint('$API_TOKEN')\nEOF", "bash <<'EOF'\ncurl -H \"x: $API_TOKEN\"\nEOF"):
            with self.subTest(cmd=cmd.split("\n")[0]):
                self.assertEqual(fx(cmd), ["env:API_TOKEN"])

    def test_heredoc_to_cat_without_a_file_still_counts(self):
        """`cat <<EOF | python3 -` feeds code to an interpreter: its reads count.

        Only a heredoc written to a file is a written script; skipping every cat heredoc would hide piped code.
        """
        cmd = "cat <<'EOF' | python3 -\nimport os; os.environ['DATABASE_URL']\nEOF"
        self.assertEqual(fx(cmd), ["env:DATABASE_URL"])


class Guards(unittest.TestCase):
    def test_git_and_gh_text_is_ignored(self):
        """The Claude Code commit heredoc, a body piped to `gh pr create --body-file -` and --title/--body
        strings give nothing.

        Commit and PR bodies carry links and names as prose; they are the biggest false-alarm source.
        """
        for cmd in (COMMIT, "cat <<'EOF' | gh pr create --body-file -\nSee https://h.example.test $API_TOKEN\nEOF",
                    'gh pr create --title "Use $API_TOKEN" --body "see https://h.example.test"',
                    'wrapx git commit -m "$(tool https://h.example.test)"',
                    'env -u GH_TOKEN -i gh pr create --body "see https://h.example.test"'):
            with self.subTest(cmd=cmd.split("\n")[0]):
                self.assertEqual(fx(cmd), [])

    def test_git_bare_word_url_and_var_still_count(self):
        """A git command's bare-word URL and $NAME still count: `git clone https://...`, `git push $REMOTE_URL`.

        Those are real calls; the guard drops only text git stores (messages and bodies).
        """
        # WHEN a clone and a push with bare words are read
        # THEN host and name
        self.assertEqual(fx("git clone https://h.example.test/x && git push $REMOTE_URL main"),
                         ["env:REMOTE_URL", "host:h.example.test"])

    def test_grep_family_pattern_ignored_by_position_and_by_flag(self):
        """grep/rg/sed/awk ignore the pattern argument (first non-flag word, or -e's value) but keep the rest;
        -f takes a file, not a pattern.

        A pattern names what is searched for, not what is touched; the file searched is touched.
        """
        cases = [("rg -n 'https://h.example.test' .env", ["envfile:.env"]),
                 ("grep -A 3 -e '$DATABASE_URL' -e \"$API_TOKEN\" prod.env", ["envfile:prod.env"]),
                 ("grep -m 1 $API_TOKEN prod.env", ["envfile:prod.env"]),
                 ("sed -n 's/$API_TOKEN/x/p' .env.local", ["envfile:.env.local"]),
                 ("awk -F= '/DATABASE_URL/ {print $2}' .env", ["envfile:.env"]),
                 ("grep -f .env.patterns $LOG_FILE", ["env:LOG_FILE", "envfile:.env.patterns"]),
                 ("wrapx grep -rn '$API_TOKEN' src/", [])]
        for cmd, want in cases:
            with self.subTest(cmd=cmd):
                self.assertEqual(fx(cmd), want)

    def test_echo_and_printf_arguments_are_ignored(self):
        """echo/printf arguments give nothing; a command after them still counts.

        Printed text is a message to the reader, not a call.
        """
        # WHEN echo and printf print URLs and names, then a real call follows
        cmd = 'echo "see https://h.example.test $API_TOKEN" && printf "%s\\n" $OTHER_KEY; curl $SVC_URL'
        # THEN only the call's name
        self.assertEqual(fx(cmd), ["env:SVC_URL"])

    def test_substitution_bodies_are_read_with_the_same_guards(self):
        """A $( ) body is read as its own command: its file counts, its grep pattern does not, and the name it
        fills is the script's own.

        `TOKEN=$(grep KEY .env | cut ...)` is how secrets get loaded; skipping bodies would lose the env file,
        and reading them without the guards would count the pattern.
        """
        cmd = "TOKEN=$(grep '$API_TOKEN' .env | cut -d= -f2) && curl -H \"x: $TOKEN\" https://h.example.test"
        self.assertEqual(fx(cmd), ["envfile:.env", "host:h.example.test"])

    def test_comments_are_ignored(self):
        """A # comment's URL or name gives nothing; the command before it still counts.

        A comment is a note to the reader, never run.
        """
        self.assertEqual(fx("curl https://a.example.test  # see https://b.example.test and $API_TOKEN"),
                         ["host:a.example.test"])

    def test_non_git_quoted_text_still_counts(self):
        """Quoted text of other commands counts: `curl -H "Authorization: Bearer $API_TOKEN"`.

        The guard is for git and gh only; dropping every quoted string would lose most real reads.
        """
        self.assertEqual(fx('curl -H "Authorization: Bearer $API_TOKEN" https://h.example.test'),
                         ["env:API_TOKEN", "host:h.example.test"])


class AssignedNames(unittest.TestCase):
    def test_names_the_shell_assigns_are_dropped(self):
        """NAME=, export NAME=, read NAME, for NAME in, local and declare NAME: the script's own variables.

        On the dev week REPO, JSON, URI, OUT and HOST showed in 11-24 sessions only because scripts set them.
        """
        # WHEN a command sets each name its own way and then reads all of them
        cmd = ("OUT_DIR=/tmp/x; export REPO_NAME=a; read -r LINE_TEXT; for ITEM_ID in a b; do :; done; "
               "local LOCAL_VAR; declare -a ARR_VAR; "
               "tool $OUT_DIR $REPO_NAME $LINE_TEXT $ITEM_ID $LOCAL_VAR $ARR_VAR $KEPT_KEY")
        # THEN only the name it never set
        self.assertEqual(fx(cmd), ["env:KEPT_KEY"])

    def test_python_assignment_does_not_hide_the_read(self):
        """A python line `DATABASE_URL = os.environ[...]` inside inline code still gives the name.

        Assigned names come from the shell level only; reading them from the code would drop exactly the
        reads the report needs.
        """
        cmd = "python3 - <<'EOF'\nimport os\nDATABASE_URL = os.environ['DATABASE_URL']\nEOF"
        self.assertEqual(fx(cmd), ["env:DATABASE_URL"])


class Speed(unittest.TestCase):
    def test_a_long_command_reads_in_linear_time(self):
        """A 200 KB command of many quoted parts and heredocs reads in well under a second.

        effects() runs on every recorded Bash call and on ~46K commands per replay; a slow regex would stall
        the recorder hook.
        """
        # GIVEN a long command
        part = "curl -H \"x: $TOKEN_A\" 'https://h.example.test' | grep -e 'aaaa' .env; "
        cmd = part * 3000 + "python3 - <<'EOF'\n" + "x = 1\n" * 3000 + "EOF"
        # WHEN read
        t = time.monotonic()
        got = fx(cmd)
        # THEN the signals, quickly
        self.assertEqual(got, ["env:TOKEN_A", "envfile:.env", "host:h.example.test"])
        self.assertLess(time.monotonic() - t, 1.5)


if __name__ == "__main__":
    unittest.main()
