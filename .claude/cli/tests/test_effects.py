"""Tests for agent_cli.effects: the hosts, env var names and .env files a Bash command touches, never values."""
import sys
import time
import unittest

from helpers import CLAUDE

sys.path.insert(0, str(CLAUDE / "cli"))
from agent_cli.effects import effects, ran  # noqa: E402

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
        # THEN the bare lowercase host, beside the db signal
        self.assertEqual(got, ["db:postgres", "host:db.example.test"])

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
        # THEN its env read, and the client import
        self.assertEqual(fx(cmd), ["db:postgres", "env:DATABASE_URL"])

    def test_node_e_and_psql_with_flags_count(self):
        """`node -e` code and `psql "$DATABASE_URL" -c ...` give the variable, whatever the flags.

        The same query is typed with varying flags; the effect is what makes those runs one chore.
        """
        self.assertEqual(fx("node -e 'const u = process.env.DATABASE_URL; console.log(u)'"), ["env:DATABASE_URL"])
        for cmd in ('psql "$DATABASE_URL" -At -c "select count(*) from t"',
                    'wrapx raw psql "$DATABASE_URL" -c "select 1"'):
            with self.subTest(cmd=cmd):
                self.assertEqual(fx(cmd), ["db:postgres", "env:DATABASE_URL"])

    def test_heredoc_written_to_a_file_is_skipped(self):
        """A heredoc redirected to a file (`cat > f <<EOF`, `cat <<EOF > f`, `tee f <<EOF`) gives nothing.

        Writing a doc or a script is not running it; the doc's URLs and variable names are text.
        """
        body = "\nSee https://docs.example.test and $API_TOKEN\nEOF"
        for cmd in ("cat > notes.md <<'EOF'" + body, "cat <<EOF > notes.md" + body, "tee notes.md <<EOF" + body):
            with self.subTest(cmd=cmd.split("\n")[0]):
                self.assertEqual(fx(cmd), [])

    def test_heredoc_written_then_run_by_the_same_command_counts(self):
        """A heredoc written to a file counts when a later step of the same command runs that file: node, tsx
        through npx or `yarn exec`, bash, the path at command position, or `source`, matched exactly or by basename.

        Dev-DB queries are often typed as `cat > /tmp/q.mjs <<'EOF' ... EOF` then `node /tmp/q.mjs`; on the dev
        week that hid DATABASE_URL in 29 sessions. The code is on the command line and the command runs it.
        """
        js = "<<'EOF'\nconst u = process.env.DATABASE_URL\nEOF\n"
        sh = "<<'EOF'\npsql \"$DATABASE_URL\" -c 'select 1'\nEOF\n"
        for cmd in ("cat > /tmp/q.mjs " + js + "node /tmp/q.mjs",
                    "cat > /tmp/q.mts " + js + "npx tsx /tmp/q.mts",
                    "cat > /tmp/q.ts " + js + "yarn exec tsx /tmp/q.ts",
                    "wrapx cat >> /tmp/q.mjs " + js + "cd /tmp && wrapx node --no-warnings q.mjs",
                    "tee -a /tmp/x.sh " + sh + "bash /tmp/x.sh",
                    "cat <<'EOF' > x.sh\npsql \"$DATABASE_URL\" -c 'select 1'\nEOF\nchmod +x x.sh && ./x.sh",
                    "cat > /tmp/x.sh " + sh + "source /tmp/x.sh"):
            with self.subTest(cmd=cmd):
                # WHEN a written-then-run heredoc is read
                # THEN its env read counts
                self.assertEqual(fx(cmd), ["env:DATABASE_URL"])

    def test_heredoc_written_but_not_run_stays_ignored(self):
        """A file heredoc gives nothing when the command runs another file, runs it only before writing it, or
        just commits it; a JS file's `${NAME}` (template text) stays text even when node runs it.

        Notes, PR bodies and docs written with `cat > f` name hosts and variables without touching them.
        """
        js = "<<'EOF'\nconst u = process.env.DATABASE_URL\nEOF\n"
        for cmd in ("cat > /tmp/a.mjs " + js + "node /tmp/b.mjs",
                    "node /tmp/q.mjs; cat > /tmp/q.mjs " + js,
                    "cat > notes.md <<EOF\nSee https://h.example.test\nEOF\ngit add notes.md",
                    "cat > /tmp/q.mjs <<'EOF'\nconsole.log(`${API_TOKEN}`)\nEOF\nnode /tmp/q.mjs"):
            with self.subTest(cmd=cmd):
                # WHEN read
                # THEN nothing
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


class Postgres(unittest.TestCase):
    """db:postgres: the command touches a Postgres database, whatever its flags or spelling."""

    def test_postgres_cli_command_word_counts_with_any_flags(self):
        """A step whose command word is a Postgres CLI gives db:postgres, past wrappers, env and timeout.

        The dev-DB query chore is typed with different flags every time; without a constant signal it only
        joined when its text happened to name $DATABASE_URL, so it never reached the weekly report.
        """
        for cmd in ("psql -At -U u -d x -c 'select 1'", "wrapx raw PGOPTIONS=y timeout 9 pg_dump -Fc x",
                    "cd /tmp && /usr/bin/pg_restore -d x f.dump", "pgcli x", "pg_dumpall -g",
                    "n=$(psql -At -c 'select 1')"):
            with self.subTest(cmd=cmd):
                # WHEN read  THEN the constant signal
                self.assertEqual(fx(cmd), ["db:postgres"])

    def test_postgres_cli_host_flag_gives_its_host_and_no_other_flag_value(self):
        """`-h h`, `--host h` and `--host=h` give host:<h> (lowercased); -U, -d and passwords are never stored.

        The host says which database a chore hits; any other flag value could be a user name or a secret.
        """
        for cmd in ("psql -h DB.Example.Test -U admin -d app", "pg_dump --host db.example.test app",
                    "pg_restore --host='db.example.test' -d app f"):
            with self.subTest(cmd=cmd):
                # WHEN read  THEN the signal and the host only
                self.assertEqual(fx(cmd), ["db:postgres", "host:db.example.test"])
        # AND a host flag that is a variable, or carries a 4-digit run, gives no host
        self.assertEqual(fx('psql -h "$PGHOST" -c x'), ["db:postgres", "env:PGHOST"])
        self.assertEqual(fx("psql -h db20240101.example.test"), ["db:postgres"])

    def test_runner_db_script_counts_in_every_runner_form(self):
        """A package runner running a `db:` script gives db:postgres, with or without run, a filter or a dir.

        Projects wrap their database chores as db:* scripts; the runner's spelling must not split them.
        """
        for cmd in ("yarn db:query", "npm run db:migrate", "yarn --filter web db:seed",
                    "yarn -C apps/web run db:push", "bun run db:studio", "npm --prefix=apps/web run db:x"):
            with self.subTest(cmd=cmd):
                # WHEN read  THEN the constant signal
                self.assertEqual(fx(cmd), ["db:postgres"])

    def test_inline_code_importing_a_postgres_client_counts(self):
        """Inline JS or Python that imports a Postgres client library gives db:postgres.

        Dev-DB queries run as `node -e` or `python3 - <<EOF` with a client library; the import is the one
        stable mark they all share.
        """
        for code in ('node -e "const { Client } = require(\\"pg\\")"', "node -e 'import pg from \"pg\"'",
                     "node --input-type=module -e 'import sql from \"postgres\"'",
                     "tsx -e 'import { neon } from \"@neondatabase/serverless\"'",  # e.g. a serverless driver
                     "python3 - <<'EOF'\nimport psycopg2\nEOF", "python3 -c 'import asyncpg'",
                     "python3 - <<'EOF'\nfrom psycopg.rows import dict_row\nEOF",
                     "cat > /tmp/q.py <<'EOF'\nimport psycopg\nEOF\npython3 /tmp/q.py"):
            with self.subTest(code=code):
                # WHEN read  THEN the constant signal
                self.assertEqual(fx(code), ["db:postgres"])

    def test_postgres_url_scheme_counts_beside_its_host(self):
        """A postgres:// or postgresql:// URL gives db:postgres as well as its host.

        A connection string in the command is the database itself, even when no CLI or library is named.
        """
        for cmd in ("some-tool 'postgres://u@db.example.test/x'", "x --url postgresql://db.example.test/x"):
            with self.subTest(cmd=cmd):
                # WHEN read  THEN the signal and the URL's host
                self.assertEqual(fx(cmd), ["db:postgres", "host:db.example.test"])

    def test_text_that_names_postgres_without_touching_it_gives_nothing(self):
        """Grep patterns, echo text, commit messages, unrun file heredocs, non-db scripts and near-miss libraries
        give no db:postgres.

        These name a database tool without running it; counting them would put review and doc work in the
        dev-DB row.
        """
        for cmd in ("grep -n psql notes.md", 'echo "run psql"', 'git commit -m "fix psql call"',
                    "cat > doc.md <<'EOF'\npsql -c 'select 1'\nimport psycopg\nEOF", "npm run test:unit",
                    "yarn dbx:seed", "node -e 'require(\"pg-format\")'", "python3 -c 'import pgvector'",
                    "# psql -c x\nls", "python3 -c 'import psycopg_pool_x'"):
            with self.subTest(cmd=cmd):
                # WHEN read  THEN nothing
                self.assertEqual(fx(cmd), [])


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

    def test_names_a_written_shell_script_assigns_are_dropped_when_a_shell_runs_it(self):
        """A heredoc-written q.sh that bash then runs drops the names it assigns (URI, SQL) and keeps the rest.

        The script's own variables are not env reads; on the dev week a written q.sh listed its URI, PGURI and
        SQL as reads because the drop only looked at the outer shell level.
        """
        # GIVEN a written q.sh that sets URI and SQL and reads API_TOKEN, run by bash in the same command
        cmd = ("cat > q.sh <<'EOF'\nURI=postgres://h.example.test/x\nSQL=\"select 1\"\n"
               "psql \"$URI\" -c \"$SQL\" $API_TOKEN\nEOF\nbash q.sh")
        # WHEN read
        # THEN only the name the script never set, plus its host and db signal
        self.assertEqual(fx(cmd), ["db:postgres", "env:API_TOKEN", "host:h.example.test"])

    def test_names_a_shell_heredoc_assigns_are_dropped(self):
        """`bash <<'EOF'` and `cat <<'EOF' | sh` drop the names their body assigns, like any shell level.

        The body is a shell script run in place; its `URI=x` then `$URI` is its own variable, not a read.
        """
        for cmd in ("bash <<'EOF'\nURI_X=h\ncurl $URI_X $API_TOKEN\nEOF",
                    "cat <<'EOF' | sh\nURI_X=h\ncurl $URI_X $API_TOKEN\nEOF"):
            with self.subTest(cmd=cmd.split("\n")[0]):
                # GIVEN a heredoc body a shell reads, which sets URI_X and reads API_TOKEN
                # WHEN read
                # THEN only the name the body never set
                self.assertEqual(fx(cmd), ["env:API_TOKEN"])

    def test_a_written_file_python_runs_keeps_its_name_equals_reads(self):
        """A written q.py run by python keeps `URI_X=os.environ['URI_X']`: only a shell assigns shell names.

        Dropping names from code a shell never reads would hide exactly the reads the report counts.
        """
        # GIVEN a written q.py whose name= line reads the same name, run by python
        cmd = "cat > q.py <<'EOF'\nimport os\nURI_X=os.environ['URI_X']\nEOF\npython3 q.py"
        # WHEN read
        # THEN the read stays
        self.assertEqual(fx(cmd), ["env:URI_X"])


class CodeMode(unittest.TestCase):
    def test_script_body_as_code_reads_code_signals_and_leaves_dollar_names_as_text(self):
        """effects(text, code=True) reads a script body as code: env reads, URLs, .env files and Postgres
        clients count, while `$NAME` in a string (Python "cost $TOTAL", JS `${API_BASE}`) is text.

        A python or node script the agent wrote is read from disk without a shell around it; reading it as a
        shell command would turn every `$` in its strings into a false env read.
        """
        # GIVEN a python script body
        body = ("import os, psycopg2\nprint(\"cost $TOTAL\")\nu = f\"${API_BASE}/x\"\n"
                "os.environ['DATABASE_URL']\nrequests.get('https://api.example.test/v1')\nopen('.env')\n")
        # WHEN read as code, and (control) as a shell command
        got, as_shell = effects(body, (), code=True), effects(body, ())
        # THEN the code signals only; the shell reading would have listed the $ names
        self.assertEqual(got, ["db:postgres", "env:DATABASE_URL", "envfile:.env", "host:api.example.test"])
        self.assertIn("env:TOTAL", as_shell)


class Ran(unittest.TestCase):
    def test_ran_lists_each_file_the_command_runs_and_whether_a_shell_runs_it(self):
        """ran() gives (path, by a shell, "") for each script a step runs, in order, past wrapper words.

        The recorder reads those files to add their steps and effects; a shell script is read as shell, any
        other as code, so the flag picks the reader.
        """
        # GIVEN a chain running a python file, a shell file three ways, and a path at command position
        cmd = "wrapx python3 -u /tmp/q.py && bash ./x.sh; source ~/env.sh; ./run.py arg; ls -la"
        # WHEN read
        # THEN each run file, in order
        self.assertEqual(ran(cmd, WRAP), [("/tmp/q.py", False, ""), ("./x.sh", True, ""), ("~/env.sh", True, ""),
                                          ("./run.py", False, "")])

    def test_ran_lists_no_file_for_inline_code(self):
        """An interpreter given inline code (-c, -e, -p, -m, --eval, --print) runs no file: ran() lists none.

        The recorder opens each listed path; `bash -c "echo hi"` listing `echo hi`, or `python3 -m mod`
        listing `mod`, would have it read whatever file happens to carry that name.
        """
        for cmd in ('bash -c "echo hi"', "sh -lc 'echo'", "python3 -m mod", "python3 -c 'print(1)'",
                    "node -e 'x()'", "node -p '1+1'", "node --eval 'x()'", "node --print=x", "perl -ne 'print' f",
                    "perl -lane 'print' f", "perl -pe 's/a/b/' f", "node -pe '1+1'", "python3 -uc 'x'",
                    "ruby -e 'p 1'", "npx tsx -e 'x()'"):
            with self.subTest(cmd=cmd):
                # GIVEN a command running inline code
                # WHEN read
                # THEN no run file
                self.assertEqual(ran(cmd, ()), [])

    def test_ran_keeps_the_file_after_flags_that_are_not_inline_code(self):
        """`bash -e x.sh`, `node --no-warnings x.mjs`, `python3 -Ximporttime x.py`, `perl -Mopen x.pl` and
        `ruby -rset x.rb` still run their file: only inline-code flags drop it.

        bash's -e is errexit, not code, and python's -X/-W and perl/ruby's -M/-r take an option or a module
        name, not code; treating them as inline code would drop real script runs.
        """
        # GIVEN runs with flags that take no code
        cmd = ("bash -ex x.sh; node --no-warnings x.mjs; python3 -u q.py; python3 -Ximporttime i.py; "
               "python3 -Wmodule w.py; perl -Mopen x.pl; ruby -rset x.rb")
        # WHEN read
        # THEN each file
        self.assertEqual(ran(cmd, ()), [("x.sh", True, ""), ("x.mjs", False, ""), ("q.py", False, ""),
                                        ("i.py", False, ""), ("w.py", False, ""), ("x.pl", False, ""),
                                        ("x.rb", False, "")])

    def test_a_flag_that_takes_the_next_word_never_gives_its_value_as_the_script(self):
        """A runner flag whose value is the next word (python -W/-X, node -r/--env-file, bash -o/-O/+o) is
        skipped with its value, so the script after it is listed.

        Taking the first non-flag word listed `ignore`, `dotenv/config` or `pipefail` as the script: the
        recorder read no file (or a wrong one) and the script's steps and effects went uncounted.
        """
        cases = [("python3 -W ignore x.py", "x.py"), ("python3 -X dev x.py", "x.py"),
                 ("python3 -Wignore x.py", "x.py"), ("python3 -Xdev x.py", "x.py"),
                 ("node -r dotenv/config x.js", "x.js"), ("node --require dotenv/config x.js", "x.js"),
                 ("node --env-file .env x.mjs", "x.mjs"), ("node --env-file=.env x.mjs", "x.mjs"),
                 ("node --import tsx/esm x.ts", "x.ts"), ("node --loader ts-node/esm x.ts", "x.ts"),
                 ("node --experimental-loader l.mjs x.mjs", "x.mjs"), ("node -C dev x.js", "x.js"),
                 ("node --conditions dev x.js", "x.js"), ("node --input-type module x.mjs", "x.mjs"),
                 ("node --title job x.js", "x.js"), ("tsx --env-file .env x.ts", "x.ts"),
                 ("ts-node -r tsconfig-paths/register x.ts", "x.ts"), ("bun --env-file .env x.ts", "x.ts"),
                 ("bun run --env-file .env x.ts", "x.ts"), ("bash -o pipefail x.sh", "x.sh"),
                 ("bash -O extglob x.sh", "x.sh"), ("bash +o posix x.sh", "x.sh"), ("bash +O extglob x.sh", "x.sh"),
                 ("bash +x x.sh", "x.sh"), ("bash --rcfile rc.sh x.sh", "x.sh"),
                 ("bash --init-file rc.sh x.sh", "x.sh")]
        for cmd, want in cases:
            with self.subTest(cmd=cmd):
                # GIVEN a run whose flag takes a value
                # WHEN read
                # THEN the script, never the flag's value
                self.assertEqual([p for p, *_ in ran(cmd, ())], [want])
        # an inline-code flag after a value flag still gives no file
        self.assertEqual(ran("python3 -X dev -m mod", ()), [])

    def test_uv_run_and_sudo_read_the_rest_as_their_own_command(self):
        """`uv run [flags] <cmd ...>` and `sudo [flags] <cmd ...>` run the command after their own flags, so
        that command's script is listed; `uv run x.py` runs x.py with python, not a shell.

        These wrappers hid the script: `sudo bash x.sh` and `uv run python x.py` listed nothing, so a chore
        run under them was never read.
        """
        cases = [("uv run python x.py", [("x.py", False, "")]), ("uv run x.py", [("x.py", False, "")]),
                 ("uv run --with requests --python 3.12 python3 x.py", [("x.py", False, "")]),
                 ("uv run -p 3.12 --project p --directory d --env-file .env x.py", [("x.py", False, "")]),
                 ("uv run --group dev --extra e --package pk --index i --with-requirements r.txt x.py",
                  [("x.py", False, "")]),
                 ("uv run pytest", []), ("uv run python -m pytest", []), ("uv run", []), ("uv run --with x", []),
                 ("sudo", []), ("sudo -v", []),
                 ("sudo bash x.sh", [("x.sh", True, "")]), ("sudo -u root python3 x.py", [("x.py", False, "")]),
                 ("sudo -g g -C 3 -D d -p pw -r r -t t -U u -h h -E bash x.sh", [("x.sh", True, "")]),
                 ("sudo -u root uv run python x.py", [("x.py", False, "")]),
                 ("npx tsx x.ts", [("x.ts", False, "")]), ("yarn exec tsx x.ts", [("x.ts", False, "")]),
                 ("deno run -A x.ts", [("x.ts", False, "")])]
        for cmd, want in cases:
            with self.subTest(cmd=cmd):
                # GIVEN a wrapped run
                # WHEN read
                # THEN the inner command's script
                self.assertEqual(ran(cmd, ()), want)

    def test_a_script_run_inside_echo_or_printf_substitution_is_listed(self):
        """A $( ) body inside an echo/printf argument still runs, so its script is listed; the echo text
        itself stays ignored for effects, while a git commit body stays ignored whole.

        `echo $(bash x.sh)` listed nothing because every echo argument was masked, bodies included.
        """
        # GIVEN scripts run in $( ) inside echo and printf
        # WHEN read
        # THEN each script
        self.assertEqual(ran("echo $(bash x.sh)", ()), [("x.sh", True, "")])
        self.assertEqual(ran("printf '%s' \"$(python3 q.py)\"", ()), [("q.py", False, "")])
        # the echo text itself still names nothing
        self.assertEqual(fx('echo "see https://h.example.test"'), [])
        # a Postgres CLI in echo's $( ) is a real run; the echoed URL text is not
        self.assertEqual(fx('echo "https://h.example.test $(psql -h db.example.test -c q)"'),
                         ["db:postgres", "host:db.example.test"])
        # a commit body's $( ) runs nothing the recorder should read
        self.assertEqual(ran('git commit -m "$(bash x.sh)"', ()), [])

    def test_a_cd_earlier_in_the_command_moves_later_relative_scripts(self):
        """A cd or pushd earlier in the same command moves later relative script paths into its dir; two cds
        compose, an absolute or ~ one replaces, and absolute or ~ run paths are never rewritten. Each joined
        path carries the dir it was joined to, which the recorder allows as a root for that read.

        The recorder resolves paths against the event's cwd, so `cd sub && python3 x.py` read <cwd>/x.py,
        the wrong file or none; and a cd into a sibling dir gave a path the cwd-only fence refused.
        """
        cases = [("cd sub && python3 x.py", "sub/x.py", "sub"),
                 ("cd /abs/dir; bash x.sh", "/abs/dir/x.sh", "/abs/dir"),
                 ("cd ~/proj && ./run.py", "~/proj/run.py", "~/proj"),
                 ("cd a && cd b && python3 x.py", "a/b/x.py", "a/b"),
                 ("cd a && cd /c && python3 x.py", "/c/x.py", "/c"),
                 ("pushd sub && bash ./x.sh", "sub/x.sh", "sub"), ("cd sub && python3 /tmp/q.py", "/tmp/q.py", ""),
                 ("cd sub && python3 ~/q.py", "~/q.py", ""), ("cd -P sub && python3 x.py", "sub/x.py", "sub"),
                 ("cd sub && echo $(bash x.sh)", "sub/x.sh", "sub")]
        for cmd, want, d in cases:
            with self.subTest(cmd=cmd):
                # GIVEN a cd, then a run
                # WHEN read
                # THEN the path from the cd's dir, and that dir (none for a path not joined to it)
                self.assertEqual([(p, x) for p, _, x in ran(cmd, ())], [(want, d)])

    def test_a_cd_to_an_unknown_dir_leaves_later_paths_as_written(self):
        """After a cd whose target is unknown ($X, a backtick or $( ), a paren, `-`, a bare cd, popd) later relative
        paths stay exactly as written, and a cd inside $( ) moves only the runs in that body.

        Guessing a dir would point the recorder at a wrong file; as written is what it read before.
        """
        cases = [("cd $D && python3 x.py", "x.py"), ('cd "$D" && python3 x.py', "x.py"),
                 ("cd `pwd` && python3 x.py", "x.py"), ("cd $(git rev-parse --show-toplevel) && python3 x.py",
                                                         "x.py"),
                 ("cd - && python3 x.py", "x.py"), ("cd && python3 x.py", "x.py"),
                 ("cd a && cd $D && python3 x.py", "x.py"), ("cd $D && cd b && python3 x.py", "x.py"),
                 ("cd $D && cd /c && python3 x.py", "/c/x.py"), ("pushd a && popd && python3 x.py", "x.py"),
                 ("echo $(cd sub; pwd); python3 x.py", "x.py"), ('cd "a(b" && python3 x.py', "x.py")]
        for cmd, want in cases:
            with self.subTest(cmd=cmd):
                # GIVEN an unknown cd, then a run
                # WHEN read
                # THEN the path as written
                self.assertEqual([p for p, *_ in ran(cmd, ())], [want])
        # a path left as written was joined to no dir, so the recorder allows no extra one
        self.assertEqual(ran("cd $D && python3 x.py", ()), [("x.py", False, "")])

    def test_in_a_command_with_case_each_paren_makes_the_dir_unknown(self):
        """With the word `case` in the command, every bare paren makes the dir unknown until a / or ~ cd.

        A case pattern's `)` read as a group's closed the group early: `cd a; (cd b; case ... y) ...;; esac;
        ./x.sh)` read a/x.sh, though the shell ran a/b/x.sh. Tracking which `)` is a pattern's kept missing
        forms (`then case`, `do case`, `{ case`, `in` on its own line, `;&`, a case in `$( )`), so the scanner
        no longer tries: paths after the paren stay as written, never in a wrong dir.
        """
        body = ["case $1 in\ny) ./p.sh ;;\nesac\n", "if true; then case $1 in\ny) ./p.sh ;;\nesac\nfi\n",
                "for f in 1; do case $f in\ny) ./p.sh ;;\nesac\ndone\n", "{ case $1 in\ny) ./p.sh ;;\nesac; }\n",
                "! case $1 in\ny) ./p.sh ;;\nesac\n", "case $1\nin\ny) ./p.sh ;;\nesac\n",
                "case $1 in\ny) case $2 in\nz) ./q.sh ;;\nesac ;;\nesac\n", "case $1 in\ny) ;&\nz) ;;\nesac\n",
                "v=$(case $1 in y) echo 1;; esac); ", "case $1 in (y) ./p.sh ;; esac; "]
        for b in body:
            with self.subTest(body=b):
                # GIVEN a case inside a group with a cd, then a run
                got = [r for r in ran("cd a; (cd b; " + b + "./x.sh)", ()) if r[0].endswith("x.sh")]
                # THEN the run stays as written: never a/x.sh
                self.assertEqual(got, [("./x.sh", True, "")])
        # AND a top-level case loses the fold too (as written, as before cds were folded); a / cd restores it
        self.assertEqual(ran("cd sub; case $x in a) ;; esac; python3 x.py", ()), [("x.py", False, "")])
        self.assertEqual(ran("case $x in a) ;; esac; cd /w; python3 x.py", ()), [("/w/x.py", False, "/w")])
        self.assertEqual(ran("cd sub; python3 case.py", ()), [("sub/case.py", False, "sub")])

    def test_a_cd_inside_a_subshell_group_moves_only_the_runs_in_that_group(self):
        """A cd inside `( )` moves the runs inside that group, up to its matching `)`, and nothing after it;
        groups nest, and a quoted paren is no group paren.

        The subshell's cd dies with it: folding it into later steps read other/scripts/x.sh for
        `(cd ../other && git pull) && ./scripts/x.sh`, a file never run, under a root the cd let in.
        """
        cases = [("(cd ../other && git pull) && ./scripts/x.sh", [("./scripts/x.sh", True, "")]),
                 ("(cd sub && make) && python3 q.py", [("q.py", False, "")]),
                 ("(cd sub && python3 x.py); python3 y.py", [("sub/x.py", False, "sub"), ("y.py", False, "")]),
                 ("(cd sub && python3 x.py)", [("sub/x.py", False, "sub")]),
                 ("cd a && (cd b && python3 x.py) && python3 y.py", [("a/b/x.py", False, "a/b"),
                                                                       ("a/y.py", False, "a")]),
                 ("(cd sub\npython3 x.py)\npython3 y.py", [("sub/x.py", False, "sub"), ("y.py", False, "")]),
                 ('(cd sub && echo ")" && python3 x.py); python3 y.py', [("sub/x.py", False, "sub"),
                                                                         ("y.py", False, "")])]
        for cmd, want in cases:
            with self.subTest(cmd=cmd):
                # GIVEN a cd in a group, then runs
                # WHEN read
                # THEN only the runs in the group are moved
                self.assertEqual(ran(cmd, ()), want)

    def test_a_cd_in_a_pipeline_or_in_the_background_moves_nothing(self):
        """A cd that is one command of a pipeline of 2+ commands, or is ended by `&` (not `&&`), runs in its
        own subshell, so later relative paths stay as written; a redirect's `&` is no background.

        `cd sub | cat; python3 x.py` runs <cwd>/x.py; joining it to sub read a file the shell never ran.
        """
        cases = [("cd sub | cat; python3 x.py", "x.py"), ("cat f | cd sub && python3 x.py", "x.py"),
                 ("cd sub & python3 x.py", "x.py"), ("cd sub &\npython3 x.py", "x.py"),
                 ("cd sub && python3 x.py | cat", "sub/x.py"), ("cd sub 2>&1 && python3 x.py", "sub/x.py"),
                 ("cd sub &>/dev/null && python3 x.py", "sub/x.py")]
        for cmd, want in cases:
            with self.subTest(cmd=cmd):
                # GIVEN a piped or backgrounded cd, then a run
                # WHEN read
                # THEN the path as the shell runs it
                self.assertEqual([p for p, *_ in ran(cmd, ())], [want])

    def test_runs_before_and_after_a_cd_keep_their_order_and_dirs(self):
        """A run before a cd stays as written; one after it is joined; a `cd ..` back to the start leaves the
        dir "." (normalised), so the next path names the file in the cwd; a quoted target with a space resolves.

        A run before a cd moved, or `sub/..` kept as the dir, would point the recorder at the wrong file.
        """
        # GIVEN runs on both sides of a cd
        # WHEN read
        # THEN each in order, with its own dir
        self.assertEqual(ran("python3 a.py && cd sub && python3 b.py", ()),
                         [("a.py", False, ""), ("sub/b.py", False, "sub")])
        self.assertEqual(ran("cd sub && python3 a.py && cd .. && python3 b.py", ()),
                         [("sub/a.py", False, "sub"), ("b.py", False, ".")])
        self.assertEqual(ran('cd "my dir" && python3 x.py', ()), [("my dir/x.py", False, "my dir")])

    def test_sudo_skips_its_env_assignments_before_the_command(self):
        """`sudo VAR=x ... <cmd>` sets env vars for cmd: the VAR=x words are skipped, so cmd's script is listed.

        `sudo FOO=1 bash x.sh` listed nothing, so the script's steps and effects went uncounted.
        """
        cases = [("sudo FOO=1 bash x.sh", [("x.sh", True, "")]),
                 ("sudo -E FOO=1 BAR=2 python3 x.py", [("x.py", False, "")]), ("sudo FOO=1", [])]
        for cmd, want in cases:
            with self.subTest(cmd=cmd):
                # GIVEN sudo with env assignments
                # WHEN read
                # THEN the inner command's script
                self.assertEqual(ran(cmd, ()), want)

    def test_a_substitution_inside_echo_counts_in_full(self):
        """The body of a $( ) inside an echo/printf argument is a real command: its URLs, env reads and .env
        names count, while the echo text around it stays ignored and a git body stays ignored whole.

        Masking the whole echo argument hid `echo $(curl https://api.example.test/x)`'s host.
        """
        # GIVEN calls inside echo/printf substitutions
        # WHEN read
        # THEN their effects
        self.assertEqual(fx("echo $(curl https://api.example.test/x)"), ["host:api.example.test"])
        self.assertEqual(fx('printf "%s see https://h.example.test" "$(cat .env; tool $TOKEN_A)"'),
                         ["env:TOKEN_A", "envfile:.env"])
        self.assertEqual(fx('echo "see https://h.example.test"'), [])
        self.assertEqual(fx('git commit -m "$(curl https://api.example.test/x)"'), [])


class SingleQuotes(unittest.TestCase):
    """`$NAME` inside single quotes is text unless the command is a shell, ssh or eval, which expand it."""

    def test_dollar_in_a_single_quoted_secret_never_becomes_a_name(self):
        """A `$` inside a single-quoted password, URL, header or inline code gives no env name.

        No shell expands single quotes, so the text after `$` is part of a value: storing it as env:<NAME>
        would write a piece of a password into a plain file under the home dir.
        """
        cases = [("export PGPASSWORD='Hx9$KQ7PZR2LM'; psql -h ep-a.example.test", "KQ7PZR2LM",
                  ["db:postgres", "host:ep-a.example.test"]),
                 ("psql 'postgresql://owner:Ab$CDEFGH@ep-a.example.test/db'", "CDEFGH",
                  ["db:postgres", "host:ep-a.example.test"]),
                 ("curl -u 'admin:Pa$SW0RDX' https://api.example.test", "SW0RDX", ["host:api.example.test"]),
                 ("curl -H 'Authorization: Bearer abc$TOKENPART' https://api.example.test", "TOKENPART",
                  ["host:api.example.test"]),
                 ("python3 -c 'print(\"$ABCSECRET\")'", "ABCSECRET", [])]
        for cmd, secret, want in cases:
            with self.subTest(secret=secret):
                # WHEN the command is read
                got = fx(cmd)
                # THEN no effect holds the secret's fragment, and only the real signals remain
                self.assertNotIn(secret, " ".join(got))
                self.assertEqual(got, want)

    def test_code_env_reads_and_shell_expanded_names_still_count(self):
        """process.env in single-quoted inline code counts, and `$NAME` counts under bash -c, ssh and eval.

        Those are real reads: the code asks for the variable, or the shell given the string expands it.
        """
        cases = [("node -e 'const u = process.env.DATABASE_URL'", "env:DATABASE_URL"),
                 ("bash -c 'echo $FOO_BAR'", "env:FOO_BAR"), ("sh -c 'tool $FOO_BAR'", "env:FOO_BAR"),
                 ("ssh box 'tool $FOO_BAR'", "env:FOO_BAR"), ("eval 'tool $FOO_BAR'", "env:FOO_BAR")]
        for cmd, want in cases:
            with self.subTest(cmd=cmd):
                # WHEN the command is read
                # THEN the name
                self.assertEqual(fx(cmd), [want])

    def test_backslash_escaped_dollar_is_text(self):
        """`\\$NAME` is never expanded, so the text after it is part of a value; `\\\\$NAME` still expands.

        A password written `pa\\$SW0RDX` in double quotes or bare would otherwise leave SW0RDX as an env name.
        """
        cases = [('PGPASSWORD="pa\\$SW0RDX" psql -h db.example.test', ["db:postgres", "host:db.example.test"]),
                 ("PGPASSWORD=pa\\$SW0RDX psql -h db.example.test", ["db:postgres", "host:db.example.test"]),
                 ('curl -u "admin:Pa\\$SW0RDX" https://h.example.test', ["host:h.example.test"])]
        for cmd, want in cases:
            with self.subTest(cmd=cmd):
                # WHEN the command is read
                got = fx(cmd)
                # THEN no fragment of the password, only the real signals
                self.assertNotIn("SW0RDX", " ".join(got))
                self.assertEqual(got, want)
        # an escaped backslash before a real expansion: the name is read
        self.assertEqual(fx('tool "a\\\\$FOO_BAR"'), ["env:FOO_BAR"])

    def test_apostrophe_in_a_comment_of_a_shell_read_heredoc_opens_no_quote(self):
        """An apostrophe in a `#` comment of a heredoc a shell reads does not start a single-quoted span.

        Taken as a quote, `# don't stop` would turn every `$NAME` up to the next `'` into text and hide the
        real reads of a multi-line chore script, the kind --by effect exists to count.
        """
        # GIVEN a quoted heredoc fed to bash whose comment holds an apostrophe, then a real read
        cmd = "bash <<'EOF'\n# don't stop\ncurl -H \"x: $API_TOKEN_X\" https://api.example.test\necho 'done'\nEOF"
        # WHEN the command is read
        got = fx(cmd)
        # THEN the read after the comment still counts
        self.assertEqual(got, ["env:API_TOKEN_X", "host:api.example.test"])
        # and a single-quoted secret in a sourced env file is still text
        self.assertEqual(fx("cat > a.env <<'EOF'\n# it's local\nPW='x#y$SECRETZ'\nEOF\n. ./a.env"), ["envfile:a.env"])

    def test_wrapped_shell_expands_its_single_quoted_script(self):
        """A shell reached through a wrapper (`-- sh -c`, sudo, xargs) still reads the names in its script.

        Only the command word was checked, so a wrapped `sh -c '... $DATABASE_URL'` gave no read at all.
        """
        cases = ["somecli env exec --worktree w -- sh -c 'q \"$DATABASE_URL\"'",
                 "sudo -u x bash -c 'q \"$DATABASE_URL\"'", "xargs sh -lc 'q \"$DATABASE_URL\"'"]
        for cmd in cases:
            with self.subTest(cmd=cmd):
                # WHEN read
                # THEN the name
                self.assertEqual(fx(cmd), ["env:DATABASE_URL"])
        # a -c flag on a non-shell does not expand: grep's count flag leaves the pattern as text
        self.assertEqual(fx("grep -c 'x$FOO_BAR' f.txt"), [])
        # a single-quoted value before the wrapped shell stays text
        self.assertEqual(fx("tool 'pa$SECRETTAIL' -- sh -c 'q $FOO_BAR'"), ["env:FOO_BAR"])

    def test_single_quotes_in_a_quoted_heredoc_a_shell_runs_are_text(self):
        """A quoted heredoc body a shell sources or reads keeps its single-quoted spans as values.

        The shell that runs the body does not expand `'pa$SECRETTAIL'`; a bare `$DATABASE_URL` there is a read.
        """
        body = "DB_PASSWORD='pa$SECRETTAIL'\nURL=$DATABASE_URL\nEOF\n"
        cases = ["cat > a.env <<'EOF'\n" + body + ". ./a.env", "bash <<'EOF'\n" + body]
        for cmd in cases:
            with self.subTest(cmd=cmd):
                # WHEN read
                got = fx(cmd)
                # THEN the bare read counts and the single-quoted value gives nothing
                self.assertNotIn("SECRETTAIL", " ".join(got))
                self.assertIn("env:DATABASE_URL", got)


class Urls(unittest.TestCase):
    def test_password_with_slash_hash_or_query_char_gives_the_real_host(self):
        """A URL whose password holds `/`, `#` or `?` gives the host after the `@`, never the user name.

        Cutting the authority at the first `/` would store the user name as a host and split one database
        into many rows.
        """
        for sep in "/#?":
            with self.subTest(sep=sep):
                # WHEN a URL with that char in its password is read
                # THEN the real host
                self.assertEqual(fx(f"curl https://user:p{sep}ss@host.example.test/x"), ["host:host.example.test"])

    def test_at_sign_in_the_path_keeps_the_authority_host(self):
        """An `@` in a URL's path or query (an email, a scoped package) does not move the host.

        Only a `user:password` authority can hide a later `@`; a plain host or host:port stays as written.
        """
        for cmd in ("curl https://api.example.test/u/a@b.example.test",
                    "curl https://api.example.test:8443/u?mail=a@b.example.test"):
            with self.subTest(cmd=cmd):
                self.assertEqual(fx(cmd), ["host:api.example.test"])

    def test_at_sign_after_the_userinfo_never_becomes_the_host(self):
        """The host follows the first `@` after the scheme; a query that looks like userinfo gives no host.

        Taking the last `@` of the token stored a query value (`next=a@secretvalue`) or an email's domain as
        a host.
        """
        # WHEN a password holds `/` and the query holds another `@`
        got = fx("curl 'https://user:p/ss@host.example.test/x?next=a@secretvalue'")
        # THEN the real host, never the query value
        self.assertEqual(got, ["host:host.example.test"])
        # WHEN the text before the only `@` is a host, a bad port and a query
        # THEN no host: the email's domain is not one
        self.assertEqual(fx("curl 'https://host.example.test:abc/path?email=me@example.org'"), [])


class Speed(unittest.TestCase):
    def test_one_long_word_and_an_unclosed_quote_read_fast(self):
        """A 40K-char single word and a 30K-char unclosed quote each read in well under a second.

        The recorder hook has a 5s timeout; a URL scan that restarts at every char of a long word is
        quadratic and loses the whole row.
        """
        for cmd in ("a" * 40000, "curl '" + "a" * 30000, "a " * 20000, "sh " * 20000, "sudo " * 20000):
            with self.subTest(n=len(cmd)):
                # WHEN read
                t = time.monotonic()
                fx(cmd)
                # THEN quickly
                self.assertLess(time.monotonic() - t, 0.5)

    def test_many_cds_list_their_runs_in_linear_time(self):
        """A 448 KB command of 32K `cd a; ./x.sh` steps lists its runs in well under the hook's 5s timeout.

        A cd dir that grows without a cap and is normalised per run made this quadratic (4.3s).
        """
        # GIVEN many cds into a nested dir, each followed by a run
        cmd = "cd a; ./x.sh; " * 32000
        # WHEN listed
        t = time.monotonic()
        got = ran(cmd, ())
        # THEN quickly, and a dir past 4096 chars is unknown, so later paths stay as written
        self.assertLess(time.monotonic() - t, 3.0)  # quadratic was 4.3s; linear ~0.6-2s under load
        self.assertEqual(got[-1], ("./x.sh", True, ""))

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
