"""A Bash command's effect signals: the hosts it calls, the env var names it reads, the .env files it loads,
and whether it touches a Postgres database.

effects(cmd) -> sorted, de-duplicated, at most MAX_EFFECTS of `host:<h>`, `env:<NAME>`, `envfile:<basename>`,
`db:postgres`. Names only, never values: a URL gives its host (userinfo, port, path and query dropped), an env
read gives the variable's name, an env file its basename. Inline code counts (`python3 - <<'EOF'`, `python3 -c`,
`node -e`): that is how dev-DB queries run. `db:postgres` is a constant, so a database chore joins however its
flags are spelled: a step whose command word is a Postgres CLI (PG_CLI; its -h/--host value also gives
host:<h>, and no other flag value is read), a package runner running a `db:` script, inline code importing a
Postgres client library (DB_CODE, matched by exact library name), or a postgres:// or postgresql:// URL.

Text that names things without touching them is ignored (about 37% of raw hits were such false alarms): a
heredoc written to a file by cat or tee (cat > f, cat >> f, tee [-a] f), unless a later step of the same
command runs that file (an interpreter word, through npx or `<runner> exec`, then the path; the path as the command
word; `.` or `source`), matched as written or, when either side is relative, by basename; quoted strings, $( )
bodies and heredoc bodies anywhere in a pipeline that runs git or gh (commit and PR bodies; bare words there
still count); the pattern argument of grep/rg/sed/awk; every argument of echo/printf; # comments; and `$NAME`
(not os.environ or process.env) in a quoted heredoc (`<<'EOF'`) fed to anything but a shell, which no shell
expands: Python that edits a shell script (a written file counts as fed to a shell when bash/sh/zsh,
`.`/`source` or a `.sh` path at command position runs it), or in single quotes, unless the command is a
shell, ssh or eval (`psql 'postgres://u:pa$SW0RD@h'` holds a value, `bash -c 'echo $A_KEY'` a read). A URL whose
`user:password` authority holds / # or ? takes its host after the token's last @. Names the shell itself assigns in the command
(NAME=, read NAME, for NAME in, local/declare NAME) are the script's own variables and are dropped; they are
read from the shell level only, never from inside inline code.
Effects feed the weekly count (`agent-cli repeats --by effect`), never the live warning.
"""
import re

from . import core, hook
from .shape import COLON_TOOLS, DIGITS4, ENV_FILE, HOST, INTERPRETER, REDIR, prefix_len, prefix_re

MAX_EFFECTS = 32
ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]{2,63}")
Q_ = r"\\?[\"']"  # a quote, maybe escaped inside a double-quoted shell string
ENV_READ = re.compile(r"\$\{?#?([A-Z][A-Z0-9_]*)(?!\w)"
                      r"|(?:os\.environ(?:\.get\(|\[)|os\.getenv\(|ENV\[|process\.env\[)\s*" + Q_ + r"(\w+)" + Q_
                      + r"|process\.env\.(\w+)")
STOP = frozenset("""HOME PATH PWD OLDPWD USER SHELL TMPDIR TERM LANG IFS RANDOM SECONDS LINENO PPID UID
PIPESTATUS BASH_SOURCE""".split())
SHELLS = frozenset(("bash", "sh", "zsh", "dash", "ksh"))
RUNNERS = re.compile(INTERPRETER.pattern + r"|deno|bun|dash|ksh|\.|source")
FILE_WORD = re.compile(r"[^\s\"'`;&|()<>=,{}\[\]]+")
ENV_CODE = frozenset(("process.env", "import.meta.env"))  # code, not a file, though ENV_FILE matches it
ASSIGNED = re.compile(r"(?:^|[\s;&|(])(?:([A-Za-z_]\w*)\+?="
                      r"|(?:for|select)\s+([A-Za-z_]\w*)\s+in\b"
                      r"|(?:readonly|read|local|declare|typeset)((?:[^\S\n]+[^\s;&|()]+)*))")
DB = "db:postgres"
PG_CLI = frozenset(("psql", "pg_dump", "pg_dumpall", "pg_restore", "pgcli"))
DB_RUNNERS = frozenset({"npm", "yarn", "bun"} | {t[0] for t in COLON_TOOLS})
# a runner's args up to its script: flags (with the value of those that take one) and `run`, then `db:`
DB_SCRIPT = re.compile(r"(?:(?:--filter|--dir|--prefix|--workspace|-[FCw]) \S+ |-\S+ |run )*[\"']?db:")
# a Postgres client library imported in inline code (JS, then Python); the library name is matched whole
DB_CODE = re.compile(r"(?:require\(|\bfrom\s+|\bimport\s*\(?)\s*" + Q_
                     + r"(?:pg|postgres|@neondatabase/serverless)" + Q_  # e.g. a serverless driver
                     + r"|\bimport\s+pg\b(?![.-])"
                     r"|\b(?:import|from)\s+(?:[\w.]+\s*,\s*)*(?:psycopg2?|asyncpg)(?![\w-])")
NAME_ARG = re.compile(r"([A-Za-z_]\w*)(?:=.*)?")
RUN = re.compile(r"[^\\'\"`$#<>\n|&;() \t]+")  # a run of plain word chars, skipped in one step
OUT_REDIR = re.compile(r"&>>?|>>|>\||>(?!&)")
# a URL: the scheme starts a word (so a long word is scanned once), the token runs to whitespace or a quote
URL = re.compile(r"(?<![\w+.-])[A-Za-z][\w+.-]*://([^\s\"'`]*)")
AUTHORITY = re.compile(r"[^/?#]*")
EXPANDS = SHELLS | {"ssh", "eval"}  # commands that expand the `$NAME` of a single-quoted argument
# grep-family tools: (flags whose value is the pattern, flags taking another value, flags naming a pattern
# file). The first word that is not a flag is the pattern unless one of the first or last kind came first.
PATTERN_TOOLS = {**dict.fromkeys(("grep", "egrep", "fgrep"), (("-e", "--regexp"), ("-A", "-B", "-C", "-m"),
                                                              ("-f", "--file"))),
                 "rg": (("-e", "--regexp"), ("-A", "-B", "-C", "-m", "-g", "--glob", "-t", "--type", "-T", "-M"),
                        ("-f", "--file")),
                 "sed": (("-e", "--expression"), (), ("-f", "--file")),
                 **dict.fromkeys(("awk", "gawk"), ((), ("-F", "-v"), ("-f",)))}


def effects(text, prefixes=None):
    """The command's effect signals, sorted, at most MAX_EFFECTS. prefixes: wrapper words to strip before
    the command word (None reads the command-prefixes seam)."""
    if prefixes is None:
        prefixes = core.seam_lines("command-prefixes")
    spans, code, writes, runs, found = [], [], [], [], set()
    _ignore(text, prefix_re(tuple(prefixes)), 0, spans, code, (writes, runs, found))
    _unrun(writes, runs, spans, code)
    view = _cut(_cut(text, code, lambda t: t.replace("$", "_")), spans, lambda t: " ")
    for m in URL.finditer(view):
        auth = AUTHORITY.match(m[1])[0]
        if "@" not in auth and "@" in m[1] and ":" in auth and not auth.rsplit(":", 1)[1].isdigit():
            auth = AUTHORITY.match(m[1].rsplit("@", 1)[1])[0]  # user:pa/ss@host: the password held / # or ?
        _host(auth.rsplit("@", 1)[-1].split(":", 1)[0], found)
        if m[0].lower().startswith(("postgres:", "postgresql:")):
            found.add(DB)
    if DB_CODE.search(view):
        found.add(DB)
    assigned = _assigned(text)
    for m in ENV_READ.finditer(view):
        name = m[1] or m[2] or m[3]
        if ENV_NAME.fullmatch(name) and name not in STOP and name not in assigned and not DIGITS4.search(name):
            found.add("env:" + name)
    for m in FILE_WORD.finditer(view):
        base = m[0].rsplit("/", 1)[-1]
        if ENV_FILE.fullmatch(base) and base not in ENV_CODE and not DIGITS4.search(base):
            found.add("envfile:" + base)
    return sorted(found)[:MAX_EFFECTS]


def _host(raw, found, whole=False):
    """Add host:<h> to found for raw lowercased: its leading HOST run (all of it when whole), unless that has
    a 4+ digit run."""
    h = (HOST.fullmatch if whole else HOST.match)(raw.lower())
    if h and not DIGITS4.search(h[0]):
        found.add("host:" + h[0].rstrip(".-"))


def _cut(text, spans, fn):
    """text with each (start, end) span (overlaps merged) replaced by fn(its text)."""
    out, at = [], 0
    for a, b in sorted(spans):
        if b > at:
            out += [text[at:max(a, at)], fn(text[max(a, at):b])]
            at = b
    return "".join(out) + text[at:]


def _assigned(text):
    """Names the shell assigns in text, read from its code view (quoted strings and heredoc bodies gone)."""
    names = set()
    for m in ASSIGNED.finditer(hook.code_view(text)):
        names.update(n for n in m.groups()[:2] if n)
        if m[3]:
            names.update(a[1] for w in m[3].split() if (a := NAME_ARG.fullmatch(w)))
    return names


def _unrun(writes, runs, out, code):
    """Add to out each file heredoc no later step runs, and to code the quoted ones only a non-shell runs.
    writes: (at, path, heredoc spans, quoted ones); runs: (at, path, by a shell). One pass from the end, so
    `seen` holds exactly the later runs: keyed by path, by basename, and by basename of relative paths."""
    seen = {}
    for at, path, *rest in sorted(writes + runs, key=lambda e: e[0], reverse=True):
        b, rel = path.rsplit("/", 1)[-1], not path.startswith("/")
        if len(rest) == 1:
            for k in [(0, path), (1, b)] + [(2, b)] * rel:
                seen[k] = seen.get(k, False) or rest[0]
            continue
        hits = [seen[k] for k in [(0, path), (2, b)] + [(1, b)] * rel if k in seen]
        out += rest[0] if not hits else []
        code += rest[1] if hits and not any(hits) else []


def _ran(s, word, args):
    """(path, by a shell) of the file this step runs, or None: an interpreter's first non-flag argument (past
    npx, `<runner> exec` and deno/bun run), or the command word itself when it is a path."""
    name, rest = word.rsplit("/", 1)[-1], [s[a:b].strip("\"'") for a, b in args if not s[a:b].startswith("-")]
    if name == "npx" or rest[:1] == ["exec"]:
        rest = rest[rest[:1] == ["exec"]:]
        name, rest = (rest[0], rest[1:]) if rest else ("", [])
    if name in ("deno", "bun") and rest[:1] == ["run"]:
        rest = rest[1:]
    if RUNNERS.fullmatch(name):
        return (rest[0], name in SHELLS or name in (".", "source")) if rest else None
    return (word.strip("\"'"), word.endswith(".sh")) if "/" in word else None


def _ignore(s, cmd_re, base, out, code, files):
    """Append to out the (start, end) spans of s (offset by base) that name things without touching them,
    and to code the quoted heredoc bodies no shell reads (their `$NAME` is text); recurse into the $( ) and
    backtick bodies outside those spans. files: (writes, runs) for _unrun, the file heredocs and the files
    the command runs, then found, the effect set, which gets each step's db:postgres and -h host."""
    line, pipe, words, cmd = [], [], [], _Cmd()  # line: the pipelines since the last newline
    pending, start, redir, i = [], None, None, 0

    def end_word(at):
        nonlocal start, redir
        if start is None:
            return
        w = (start, at)
        if redir is not None:  # a redirect's target: not an argument
            if redir and s[start:at] != "/dev/null":
                cmd.out = s[start:at]
            redir = None
        elif s[start:at].isdigit() and s.startswith((">", "<"), at):
            pass  # the fd of `2>&1`
        else:
            words.append(w)
        start = None

    def end_cmd():
        nonlocal cmd, words
        cmd.words = words
        pipe.append(cmd)
        cmd, words = _Cmd(), []

    def end_pipe():
        nonlocal pipe
        end_cmd()
        line.append(pipe)
        pipe = []

    def end_line():
        end_word(i)
        end_pipe()
        for p in line:  # judged once the line's heredoc bodies are known
            _judge(s, p, cmd_re, base, out, code, files)
        line.clear()

    while i < len(s):
        c = s[i]
        m = RUN.match(s, i)
        if m:
            start = i if start is None else start
            i = m.end()
        elif c == "\\":
            if s.startswith("\n", i + 1):
                end_word(i)
            else:
                start = i if start is None else start
            i += 2
        elif c in "'`":
            j = s.find(c, i + 1)
            j = len(s) if j < 0 else j
            start = i if start is None else start
            if c == "'":
                cmd.single.append((i, j + 1))
            (cmd.subst if c == "`" else cmd.quoted).append((i + 1, j) if c == "`" else (i, j + 1))
            i = j + 1
        elif c == '"':
            _, j = hook._dquote(s, i + 1)
            j = min(j, len(s))
            start = i if start is None else start
            cmd.quoted.append((i, j))
            cmd.subst += _dquote_substs(s, i + 1, j - 1)
            i = j
        elif s.startswith("$(", i):
            _, j = hook._scan(s, i + 2, True)
            j = min(j, len(s))
            start = i if start is None else start
            cmd.subst.append((i + 2, max(i + 2, j - 1)))
            i = j
        elif c == "$":
            start = i if start is None else start
            i += 1
        elif c == "#" and start is None:
            nl = s.find("\n", i)
            nl = len(s) if nl < 0 else nl
            out.append((base + i, base + nl))
            i = nl
        elif s.startswith("<<", i) and not s.startswith("<<<", i) and hook.HEREDOC.match(s, i):
            end_word(i)
            m = hook.HEREDOC.match(s, i)
            pending.append((next(g for g in m.groups()[1:] if g is not None), m[1] == "-", cmd,
                            any(q in m[0] for q in "'\"\\")))  # a quoted delimiter: the body is not expanded
            i = m.end()
        elif c == "\n":
            end_word(i)
            i += 1
            for delim, dash, owner, quoted in pending:
                j = hook._skip_heredocs(s, i, [(delim, dash)])
                owner.heredocs.append((i, j))
                if quoted:
                    owner.literal.append((i, j))
                i = j
            pending = []
            end_line()
        elif c in "<>" or s.startswith("&>", i):
            end_word(i)
            op = REDIR.match(s, i)[0]
            i += len(op)
            if op.endswith("&"):
                fd = re.match(r"\d+-?|-", s[i:])
                i += len(fd[0]) if fd else 0
            else:
                redir = bool(OUT_REDIR.fullmatch(op))
        elif c in "|&;":
            end_word(i)
            two = s[i:i + 2]
            if c == "|" and two != "||":
                end_cmd()
                i += 1 + (two == "|&")
            else:
                end_pipe()
                i += 2 if two in ("||", "&&", ";;") else 1
        else:  # space, tab, ( and )
            end_word(i)
            i += 1
    end_line()


class _Cmd:
    """One simple command while scanning: its word spans, quoted (and single-quoted) and $( ) body spans,
    heredoc body spans (and those with a quoted delimiter, in literal), and the file it redirects output to
    ("" for none)."""

    def __init__(self):
        self.words, self.quoted, self.single, self.subst, self.heredocs, self.literal = [], [], [], [], [], []
        self.out = ""


def _judge(s, pipe, cmd_re, base, out, code, files):
    """Add a finished pipeline's ignored spans to out and its unexpanded code to code, its file heredocs and
    run files and db signals to files, then recurse into its bodies that stay."""
    named = [(c, _command(s, c.words, cmd_re)) for c in pipe]
    git = any(name in ("git", "gh") for _, (name, *_) in named)
    shell = any(name in SHELLS for _, (name, *_) in named)
    for c, (name, args, word) in named:
        at = base + (c.words[0][0] if c.words else 0)
        literal = [] if shell else [(base + a, base + b) for a, b in c.literal]
        spans = c.quoted + c.subst + c.heredocs if git else []
        if name in ("echo", "printf"):
            spans += args
        elif name in PATTERN_TOOLS:
            spans += _patterns(s, args, *PATTERN_TOOLS[name])
        target = c.out if name == "cat" else next((s[a:b] for a, b in args if s[a] != "-"), "")
        if name in ("cat", "tee") and target and c.heredocs:  # kept only if a later step runs it: _unrun
            files[0].append((at, target.strip("\"'"), [(base + a, base + b) for a, b in c.heredocs], literal))
        else:
            code += literal
        if name not in EXPANDS:  # no shell expands single quotes: `'pa$SW0RD'` is a value, not a read
            code += [(base + a, base + b) for a, b in c.single]
        if run := word and _ran(s, word, args):
            files[1].append((at, *run))
        ws = [s[a:b] for a, b in args]
        # the first 6 args only: the script comes by then, and DB_SCRIPT's alternatives overlap (backtracking)
        if name in PG_CLI or name in DB_RUNNERS and DB_SCRIPT.match(" ".join(ws[:6]) + " "):
            files[2].add(DB)
        for k, t in enumerate(ws if name in PG_CLI else ()):  # -h/--host's value only: never -U, -d or others
            if t in ("-h", "--host") and k + 1 < len(ws) or t.startswith("--host="):
                _host((t[7:] if t.startswith("--host=") else ws[k + 1]).strip("\"'"), files[2], True)
        out += [(base + a, base + b) for a, b in spans]
        for a, b in c.subst:
            if not any(x <= a and b <= y for x, y in spans):
                _ignore(s[a:b], cmd_re, base + a, out, code, files)


def _command(s, words, cmd_re):
    """(command name, argument word spans, command word) past keywords, VAR=x and prefix words (shape.prefix_len)."""
    k = prefix_len([s[a:b] for a, b in words], cmd_re)
    if k >= len(words):
        return None, [], None
    a, b = words[k]
    return s[a:b].rsplit("/", 1)[-1], words[k + 1:], s[a:b]


def _patterns(s, args, pattern_flags, value_flags, file_flags):
    """The spans of a grep-family tool's pattern words among args."""
    out, given, skip = [], False, None
    for a, b in args:
        t = s[a:b]
        if skip:
            if skip == "pattern":
                out.append((a, b))
            skip = None
        elif t in pattern_flags or t in file_flags:
            given, skip = True, "pattern" if t in pattern_flags else "value"
        elif t in value_flags:
            skip = "value"
        elif any(t.startswith(f + "=") for f in pattern_flags if f.startswith("--")) or (
                "-e" in pattern_flags and t.startswith("-e") and len(t) > 2):
            given = True
            out.append((a, b))
        elif t.startswith("-") and t != "-":
            continue
        elif not given:
            given = True
            out.append((a, b))
    return out


def _dquote_substs(s, i, end):
    """The $( ) body spans inside a double-quoted string s[i:end]."""
    out = []
    while i < end:
        if s[i] == "\\":
            i += 2
        elif s.startswith("$(", i):
            _, j = hook._scan(s, i + 2, True)
            out.append((i + 2, max(i + 2, min(j, end) - 1)))
            i = j
        else:
            i += 1
    return out
