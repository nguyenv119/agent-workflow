"""A Bash command's effect signals: the hosts it calls, the env var names it reads, the .env files it loads.

effects(cmd) -> sorted, de-duplicated, at most MAX_EFFECTS of `host:<h>`, `env:<NAME>`, `envfile:<basename>`.
Names only, never values: a URL gives its host (userinfo, port, path and query dropped), an env read gives
the variable's name, an env file its basename. Inline code counts (`python3 - <<'EOF'`, `python3 -c`,
`node -e`): that is how dev-DB queries run.

Text that names things without touching them is ignored (about 37% of raw hits were such false alarms):
a heredoc written to a file by cat or tee; quoted strings, $( ) bodies and heredoc bodies anywhere in a
pipeline that runs git or gh (commit and PR bodies; bare words there still count); the pattern argument of
grep/rg/sed/awk; every argument of echo/printf; # comments; and `$NAME` (not os.environ or process.env)
in a quoted heredoc (`<<'EOF'`) fed to anything but a shell, which no shell expands: Python that edits a
shell script. Names the shell itself assigns in the command (NAME=, read NAME, for NAME in, local/declare
NAME) are the script's own variables and are dropped; they are read from the shell level only, never from
inside inline code. Effects feed the weekly count (`agent-cli repeats --by effect`), never the live warning.
"""
import re

from . import core, hook
from .shape import DIGITS4, ENV_FILE, HOST, URL, prefix_re

MAX_EFFECTS = 32
ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]{2,63}")
Q_ = r"\\?[\"']"  # a quote, maybe escaped inside a double-quoted shell string
ENV_READ = re.compile(r"\$\{?#?([A-Z][A-Z0-9_]*)(?!\w)"
                      r"|(?:os\.environ(?:\.get\(|\[)|os\.getenv\(|ENV\[|process\.env\[)\s*" + Q_ + r"(\w+)" + Q_
                      + r"|process\.env\.(\w+)")
STOP = frozenset("""HOME PATH PWD OLDPWD USER SHELL TMPDIR TERM LANG IFS RANDOM SECONDS LINENO PPID UID
PIPESTATUS BASH_SOURCE""".split())
SHELLS = frozenset(("bash", "sh", "zsh", "dash", "ksh"))
FILE_WORD = re.compile(r"[^\s\"'`;&|()<>=,{}\[\]]+")
ENV_CODE = frozenset(("process.env", "import.meta.env"))  # code, not a file, though ENV_FILE matches it
ASSIGNED = re.compile(r"(?:^|[\s;&|(])(?:([A-Za-z_]\w*)\+?="
                      r"|(?:for|select)\s+([A-Za-z_]\w*)\s+in\b"
                      r"|(?:readonly|read|local|declare|typeset)((?:[^\S\n]+[^\s;&|()]+)*))")
NAME_ARG = re.compile(r"([A-Za-z_]\w*)(?:=.*)?")
RUN = re.compile(r"[^\\'\"`$#<>\n|&;() \t]+")  # a run of plain word chars, skipped in one step
OUT_REDIR = re.compile(r"&>>?|>>|>\||>(?!&)")
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
    spans, code = [], []
    _ignore(text, prefix_re(tuple(prefixes)), 0, spans, code)
    view = _cut(_cut(text, code, lambda t: t.replace("$", " ")), spans, lambda t: " ")
    found = set()
    for m in URL.finditer(view):
        h = HOST.match(m[1].rsplit("@", 1)[-1].split(":", 1)[0].lower())
        if h and not DIGITS4.search(h[0]):
            found.add("host:" + h[0].rstrip(".-"))
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


def _ignore(s, cmd_re, base, out, code):
    """Append to out the (start, end) spans of s (offset by base) that name things without touching them,
    and to code the quoted heredoc bodies no shell reads (their `$NAME` is text); recurse into the $( ) and
    backtick bodies outside those spans."""
    line, pipe, words, cmd = [], [], [], _Cmd()  # line: the pipelines since the last newline
    pending, start, redir, i = [], None, None, 0

    def end_word(at):
        nonlocal start, redir
        if start is None:
            return
        w = (start, at)
        if redir is not None:  # a redirect's target: not an argument
            cmd.out |= redir and s[start:at] != "/dev/null"
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
            _judge(s, p, cmd_re, base, out, code)
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
            op = re.match(r"&>>?|>>|>\||>&|<&|<>|<<<|>|<", s[i:])[0]
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
    """One simple command while scanning: its word spans, quoted and $( ) body spans, heredoc body spans (and
    those with a quoted delimiter, in literal), and whether it redirects output to a file."""

    def __init__(self):
        self.words, self.quoted, self.subst, self.heredocs, self.literal, self.out = [], [], [], [], [], False


def _judge(s, pipe, cmd_re, base, out, code):
    """Add a finished pipeline's ignored spans to out and its unexpanded code to code, then recurse into its
    bodies that stay."""
    named = [(c, _command(s, c.words, cmd_re)) for c in pipe]
    git = any(name in ("git", "gh") for _, (name, _) in named)
    shell = any(name in SHELLS for _, (name, _) in named)
    for c, (name, args) in named:
        if not shell:
            code += [(base + a, base + b) for a, b in c.literal]
        spans = c.quoted + c.subst + c.heredocs if git else []
        if name in ("echo", "printf"):
            spans += args
        elif name in PATTERN_TOOLS:
            spans += _patterns(s, args, *PATTERN_TOOLS[name])
        if name in ("cat", "tee") and (c.out or name == "tee"):
            spans += c.heredocs
        out += [(base + a, base + b) for a, b in spans]
        for a, b in c.subst:
            if not any(x <= a and b <= y for x, y in spans):
                _ignore(s[a:b], cmd_re, base + a, out, code)


def _command(s, words, cmd_re):
    """(command name, argument word spans) past keywords, VAR=x and prefix words, as shape._strip does, and
    past a prefix's own options (`env -u A gh`: no command word starts with -)."""
    k = 0
    while k < len(words):
        rep = " ".join(s[a:b].replace(" ", "_").replace("\n", "_") for a, b in words[k:k + 16]) + " "
        n = len(rep[:cmd_re.match(rep).end()].split())
        while k + n < len(words) and s[slice(*words[k + n])].startswith("-"):
            n += 1 + (s[slice(*words[k + n])] in ("-u", "--unset", "-C", "--chdir", "-S"))
        if not n:
            break
        k += n
    if k >= len(words):
        return None, []
    a, b = words[k]
    return s[a:b].rsplit("/", 1)[-1], words[k + 1:]


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
