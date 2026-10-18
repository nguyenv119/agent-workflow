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
still count); the pattern argument of grep/rg/sed/awk; every argument of echo/printf (a $( ) in one still
runs and counts); # comments; and `$NAME`
(not os.environ or process.env) in a quoted heredoc (`<<'EOF'`) fed to anything but a shell, which no shell
expands: Python that edits a shell script (a written file counts as fed to a shell when bash/sh/zsh,
`.`/`source` or a `.sh` path at command position runs it), or in single quotes, unless an earlier word of
the step is ssh, eval or a shell given -c, wrapped or not (`psql 'postgres://u:pa$SW0RD@h'` holds a value,
`sudo bash -c 'echo $A_KEY'` a read), or in single quotes inside a quoted heredoc a shell reads, or after a
backslash (`pa\\$SW0RD`). A URL whose `user:password` authority holds / # or ? takes its host after the first
@, and none when the text before that @ holds = or & (a query). Names the shell itself assigns in the command
(NAME=, read NAME, for NAME in, local/declare NAME) are the script's own variables and are dropped; they are
read from the shell level and from heredoc bodies a shell reads (`bash <<EOF`, or a written file a shell
runs), never from inside inline code. effects(text, code=True) reads a script body no shell reads (a .py
file the recorder opens) as one code span: URLs, code env reads, .env words and DB_CODE count, `$NAME` and
assignments do not. ran(text) lists the files the command runs (past flag values and the npx, sudo,
`uv run` wrappers; moved by an earlier cd in the same shell, with that cd's dir), for the recorder to read.
Effects feed the weekly count (`agent-cli repeats --by effect`), never the live warning.
"""
import itertools
import posixpath
import re

from . import core, hook
from .shape import ASSIGN, COLON_TOOLS, DIGITS4, ENV_FILE, HOST, INTERPRETER, REDIR, prefix_len, prefix_re

MAX_EFFECTS = 32
CASE = re.compile(r"(?<![\w-])case(?![\w-])")  # the word `case` anywhere: its pattern `)`s look like group parens
MAX_DIR = 4096  # a longer cd dir is unknown: each run joined to it costs its length
ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]{2,63}")
Q_ = r"\\?[\"']"  # a quote, maybe escaped inside a double-quoted shell string
# `\$X` is never expanded; `\\$X` is (an odd run of 3+ backslashes still reads as a name)
ENV_READ = re.compile(r"(?<!(?<!\\)\\)\$\{?#?([A-Z][A-Z0-9_]*)(?!\w)"
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
SHELL_C = re.compile(r"-[A-Za-z]*c[A-Za-z]*")  # a shell's -c, alone or in a cluster (-lc)
# runner flags whose value is the next word (the attached -Wignore or --env-file=.env needs no skip), by
# runner kind, node's for tsx, ts-node and bun too; then the wrappers' own, before their command
_NODE = frozenset("-r --require --import --loader --experimental-loader -C --conditions --input-type --title "
                  "--env-file".split())
VALUES = {"py": ("-W", "-X"), "sh": ("-o", "-O", "+o", "+O", "--rcfile", "--init-file"),
          **dict.fromkeys(("node", "tsx", "ts-node", "bun"), _NODE),
          "uv": ("--with", "--with-requirements", "--python", "-p", "--project", "--directory", "--env-file",
                 "--group", "--extra", "--package", "--index"),
          "sudo": ("-u", "-g", "-C", "-D", "-p", "-r", "-t", "-U", "-h")}
# Flags that hand a runner inline code (or a module) instead of a file: a shell's -c, python's -c/-m after
# only its no-argument letters, and -e/-E/-p/--eval/--print for the rest (node, perl, ruby, tsx, bun) after
# only perl/ruby switch letters (-ne, -lane, -pe). bash's -e (errexit), python's -X/-W and perl/ruby's
# -M/-r (an option or module, then the file) are not.
INLINE = {"sh": SHELL_C, "py": re.compile(r"-[bBdEhiIOPqsSuvx]*[cm].*"),
          "": re.compile(r"-[lnapiw0-9]*[eEp]|--(?:eval|print)(?:=.*)?")}
# a single-quoted span, past # comments (an apostrophe in `# don't` opens nothing), escapes and "..."
SINGLE = re.compile(r"(?:^|(?<=[\s;&|(]))#[^\n]*|\\.|\"(?:\\.|[^\"\\])*\"|'[^']*'", re.S | re.M)
# grep-family tools: (flags whose value is the pattern, flags taking another value, flags naming a pattern
# file). The first word that is not a flag is the pattern unless one of the first or last kind came first.
PATTERN_TOOLS = {**dict.fromkeys(("grep", "egrep", "fgrep"), (("-e", "--regexp"), ("-A", "-B", "-C", "-m"),
                                                              ("-f", "--file"))),
                 "rg": (("-e", "--regexp"), ("-A", "-B", "-C", "-m", "-g", "--glob", "-t", "--type", "-T", "-M"),
                        ("-f", "--file")),
                 "sed": (("-e", "--expression"), (), ("-f", "--file")),
                 **dict.fromkeys(("awk", "gawk"), ((), ("-F", "-v"), ("-f",)))}


def effects(text, prefixes=None, code=False):
    """The command's effect signals, sorted, at most MAX_EFFECTS. prefixes: wrapper words to strip before
    the command word (None reads the command-prefixes seam). code: text is a script body no shell reads (a
    .py or .mjs file): one code span, where `$NAME` is text and no name is assigned."""
    spans, codes, found, assigned = [], [(0, len(text))] if code else [], set(), set()
    if not code:
        writes, runs, bodies = [], [], []
        _ignore(text, _cmd_re(prefixes), 0, spans, codes, (writes, runs, found, bodies, []))
        _unrun(writes, runs, spans, codes, bodies)
        assigned = _assigned(text).union(*(_assigned(text[a:b]) for a, b in bodies))
    view = _cut(_cut(text, codes, lambda t: t.replace("$", "_")), spans, lambda t: " ")
    for m in URL.finditer(view):
        auth = AUTHORITY.match(m[1])[0]
        if "@" not in auth and "@" in m[1] and ":" in auth and not auth.rsplit(":", 1)[1].isdigit():
            user, rest = m[1].split("@", 1)  # user:pa/ss@host: the password held / # or ?
            auth = "" if "=" in user or "&" in user else AUTHORITY.match(rest)[0]  # a query: unclear, no host
        _host(auth.rsplit("@", 1)[-1].split(":", 1)[0], found)
        if m[0].lower().startswith(("postgres:", "postgresql:")):
            found.add(DB)
    if DB_CODE.search(view):
        found.add(DB)
    for m in ENV_READ.finditer(view):
        name = m[1] or m[2] or m[3]
        if ENV_NAME.fullmatch(name) and name not in STOP and name not in assigned and not DIGITS4.search(name):
            found.add("env:" + name)
    for m in FILE_WORD.finditer(view):
        base = m[0].rsplit("/", 1)[-1]
        if ENV_FILE.fullmatch(base) and base not in ENV_CODE and not DIGITS4.search(base):
            found.add("envfile:" + base)
    return sorted(found)[:MAX_EFFECTS]


def ran(text, prefixes=None):
    """(path, by a shell, dir) of each file the command runs, in order (_ran of each step, $( ) bodies
    included); the path as written, unexpanded, but for a relative one after a cd/pushd outside $( ): joined
    to its dir (normalised), which is the third item ("" when not joined: `cd sub && ./x.sh` gives
    ("sub/x.sh", True, "sub"); cds compose, an absolute or ~ one replaces, `cd sub && cd ..` leaves ".").
    A cd inside a ( ) group moves only the runs up to that group's `)`; one in a pipeline of 2+ commands or
    ended by `&` moves nothing. After a cd to an unknown dir (a $, backtick, quote or paren left in it, `-`,
    +N, none, over MAX_DIR chars) or a popd, relative paths stay as written until an absolute or ~ cd, or
    the `)` of the group it sits in. A case pattern's `)` reads like a group's, so in a command holding the
    word `case`, each paren makes the dir unknown (as an unknown cd does)."""
    runs, moves, out, d, saved, opened = [], [], [], "", [], False
    _ignore(text, _cmd_re(prefixes), 0, [], [], ([], runs, set(), [], moves))
    case = CASE.search(text)
    for at, *e in sorted(runs + moves, key=lambda r: r[0]):  # a run: (at, path, by a shell)
        if e in (["("], [")"]) and case:  # a paren may be a group's or a case pattern's: dir unknown till a / or ~ cd
            d = None
        elif e == ["("]:  # a group saves the dir its `)` restores
            saved.append(d)
            opened = True
        elif e == [")"]:  # unmatched after a group: a stray `)`, so the dir is unknown
            d = saved.pop() if saved else None if opened else d
        elif len(e) == 1:  # a cd: (at, target)
            d = _cd(d, e[0])
        elif d and not e[0].startswith(("/", "~")):
            out.append((posixpath.normpath(posixpath.join(d, e[0])), e[1], d))
        else:
            out.append((e[0], e[1], ""))
    return out


def _cmd_re(prefixes):
    return prefix_re(tuple(core.seam_lines("command-prefixes") if prefixes is None else prefixes))


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


def _unrun(writes, runs, out, code, shell):
    """Add to out each file heredoc no later step runs, to code the quoted ones only a non-shell runs, and to
    shell the ones a shell runs. writes: (at, path, heredoc spans, quoted ones, their single-quoted spans,
    text when a shell runs the file); runs: (at, path, by a shell). One pass from the end, so
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
        code += (rest[2] if any(hits) else rest[1]) if hits else []
        shell += rest[0] if any(hits) else []


def _ran(s, word, args):
    """(path, by a shell) of the file this step runs, or None: the command word itself when it is a path, or a
    runner's first argument past its flags and the values of those that take one (VALUES: python -W ignore,
    node -r m, bash -o pipefail; a shell's +o/+x are flags too), past the wrappers npx, sudo, `uv run` and
    `<runner> exec`, each read as its own command (`uv run x.py` runs x.py with python; `sudo FOO=1 cmd`
    skips FOO=1), and deno/bun run.
    None too when a flag before that argument gives inline code (INLINE: bash -c, python3 -m, node
    -e/-p/--eval, perl -ne): such a run reads no file."""
    return _run(word.strip("\"'"), [s[a:b].strip("\"'") for a, b in args])


def _run(word, ws):
    """_ran of the command word word and its argument words ws, quotes stripped. A loop, not recursion, and
    ws is never copied: `sudo sudo ...` stays linear."""
    j, uv = 0, False  # ws[j:]: word's arguments; uv: word is what `uv run` runs
    while True:
        name = word.rsplit("/", 1)[-1]
        values = VALUES.get(name, ())
        k = _arg(ws, j, values, env=name == "sudo")  # `sudo FOO=1 cmd`
        if k is None or not (name in ("npx", "sudo") or ws[k] == "exec" or name == "uv" and ws[k] == "run"):
            break
        k = k if name in ("npx", "sudo") else _arg(ws, k + 1, values)
        if k is None:
            return None
        word, j, uv = ws[k], k + 1, name == "uv"
    if not RUNNERS.fullmatch(name):
        return (word, False) if uv and word.endswith(".py") else (word, word.endswith(".sh")) if "/" in word else None
    kind = "sh" if name in SHELLS else "py" if name.startswith("python") else ""
    values, inline = VALUES.get(kind or name, ()), INLINE[kind]
    k = _arg(ws, j, values, inline, kind == "sh")
    if name in ("deno", "bun") and k is not None and ws[k] == "run":
        k = _arg(ws, k + 1, values, inline)
    return None if k is None else (ws[k], name in SHELLS or name in (".", "source"))


def _arg(ws, k, values, inline=None, plus=False, env=False):
    """Where the first word of ws from k sits that is neither a flag (`+x` too, when plus; VAR=x too, when
    env) nor the value of one in values, or None: none left, or an inline-code flag (inline) comes first."""
    while k < len(ws):
        w = ws[k]
        if inline and inline.fullmatch(w):
            return None
        if w in values:
            k += 2
        elif w.startswith("-") or plus and w.startswith("+") or env and ASSIGN.match(w):
            k += 1
        else:
            return k
    return None


def _cd(d, t):
    """The dir after `cd t` from d, normalised ("" for as written, None for unknown: t None, a relative t
    from an unknown d, or a result over MAX_DIR chars)."""
    if t is None or d is None and not t.startswith(("/", "~")):
        return None
    n = posixpath.normpath(t if t.startswith(("/", "~")) else posixpath.join(d, t))
    return n if len(n) <= MAX_DIR else None


def _ignore(s, cmd_re, base, out, code, files):
    """Append to out the (start, end) spans of s (offset by base) that name things without touching them,
    and to code the quoted heredoc bodies no shell reads (their `$NAME` is text); recurse into the $( ) and
    backtick bodies outside those spans (an echo/printf argument's included: it still runs). files: (writes,
    runs) for _unrun, the file heredocs and the files the command runs, then found, the effect set, which gets
    each step's db:postgres and -h host, then the spans of heredoc bodies a shell in the pipeline reads (their
    assignments are shell names), then for ran() the cd steps outside $( ) that are alone in their pipeline
    and not ended by `&`, (at, dir or None when unknown), and the bare `(` and `)` outside $( ), (at, paren)."""
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
                cmd.bg = c == "&" and two != "&&"  # `cd x &` runs in a subshell
                end_pipe()
                i += 2 if two in ("||", "&&", ";;") else 1
        else:  # space, tab, ( and ): a bare paren opens or closes a subshell group (quoted ones were consumed)
            end_word(i)
            if c in "()" and not base:
                files[4].append((i, c))
            i += 1
    end_line()


class _Cmd:
    """One simple command while scanning: its word spans, quoted (and single-quoted) and $( ) body spans,
    heredoc body spans (and those with a quoted delimiter, in literal), the file it redirects output to
    ("" for none), and whether a lone `&` ends it (bg)."""

    def __init__(self):
        self.words, self.quoted, self.single, self.subst, self.heredocs, self.literal = [], [], [], [], [], []
        self.out, self.bg = "", False


def _judge(s, pipe, cmd_re, base, out, code, files):
    """Add a finished pipeline's ignored spans to out and its unexpanded code to code, its file heredocs and
    run files and db signals to files, then recurse into its bodies that stay."""
    named = [(c, _command(s, c.words, cmd_re)) for c in pipe]
    git = any(name in ("git", "gh") for _, (name, *_) in named)
    shell = any(name in SHELLS for _, (name, *_) in named)
    for c, (name, args, word) in named:
        at = base + (c.words[0][0] if c.words else 0)
        singles = [(base + x, base + y) for a, b in c.literal for m in SINGLE.finditer(s, a, b)
                   if m[0][0] == "'" for x, y in [m.span()]]  # a shell reading the body leaves these as text
        literal = singles if shell else [(base + a, base + b) for a, b in c.literal]
        spans = c.quoted + c.subst + c.heredocs if git else []
        if name in PATTERN_TOOLS:
            spans += _patterns(s, args, *PATTERN_TOOLS[name])
        target = c.out if name == "cat" else next((s[a:b] for a, b in args if s[a] != "-"), "")
        if name in ("cat", "tee") and target and c.heredocs:  # kept only if a later step runs it: _unrun
            files[0].append((at, target.strip("\"'"), [(base + a, base + b) for a, b in c.heredocs], literal,
                             singles))
        else:
            code += literal
            files[3].extend((base + a, base + b) for a, b in c.heredocs if shell)
        p = _expander(s, c.words)  # no shell expands single quotes before it: `'pa$SW0RD'` is a value
        code += [(base + a, base + b) for a, b in c.single if a < p]
        if run := word and _ran(s, word, args):
            files[1].append((at, *run))
        ws = [s[a:b] for a, b in args]
        # outside $( ), alone in its pipeline and not backgrounded: any other cd runs in a subshell
        if name in ("cd", "pushd", "popd") and not base and len(pipe) == 1 and not c.bg:
            d = "" if name == "popd" else next((w.strip("\"'") for w in ws if not w.startswith("-") or w == "-"), "")
            files[4].append((at, None if not d or d == "-" or re.search(r"[$`'\"()]|^\+", d) else d))
        # the first 6 args only: the script comes by then, and DB_SCRIPT's alternatives overlap (backtracking)
        if name in PG_CLI or name in DB_RUNNERS and DB_SCRIPT.match(" ".join(ws[:6]) + " "):
            files[2].add(DB)
        for k, t in enumerate(ws if name in PG_CLI else ()):  # -h/--host's value only: never -U, -d or others
            if t in ("-h", "--host") and k + 1 < len(ws) or t.startswith("--host="):
                _host((t[7:] if t.startswith("--host=") else ws[k + 1]).strip("\"'"), files[2], True)
        # echo/printf text names nothing, but a $( ) in it still runs: its body is left out, and read below
        out += [(base + a, base + b) for a, b in spans + (_minus(args, c.subst) if name in ("echo", "printf") else [])]
        for a, b in c.subst:
            if not any(x <= a and b <= y for x, y in spans):
                _ignore(s[a:b], cmd_re, base + a, out, code, files)


def _minus(spans, holes):
    """spans with the holes inside them cut out; both in order, so one pass."""
    out, k = [], 0
    for a, b in spans:
        while k < len(holes) and holes[k][1] <= b:
            if holes[k][0] >= a:
                out.append((a, holes[k][0]))
                a = holes[k][1]
            k += 1
        out.append((a, b))
    return out


def _expander(s, words):
    """Where the first word that expands later single-quoted `$NAME`s starts (ssh, eval, or a shell given -c
    in its leading flags), or len(s): any word of the step, so a wrapped `-- sh -c '...'` counts."""
    ws = [s[a:b].rsplit("/", 1)[-1] for a, b in words]
    for k, w in enumerate(ws):
        flags = itertools.takewhile(lambda t: t.startswith("-"), map(ws.__getitem__, range(k + 1, len(ws))))
        if w in ("ssh", "eval") or w in SHELLS and any(SHELL_C.fullmatch(t) for t in flags):
            return words[k][0]
    return len(s)


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
