"""A Bash command's shape: command and subcommand names and flag names kept, every value masked.

`git -C /x log -5 "a b"` -> `git -C <path> log ...`: quoted text -> <str>, heredoc bodies -> <heredoc>,
numbers -> <n>, hex ids and UUIDs -> <id>, anything with / -> <path>, URLs -> <url:host>, X=value ->
X=<v>, a flag's value -> <v>, other bare words -> <arg>. Leading `cd <x> &&` chains and the prefixes
hook_rules.cmd_re knows (env, timeout N, VAR=x and the project's command-prefixes seam words) are
stripped. The shape never holds a value from the command, so it is safe to store.
"""
import functools
import re

from . import core, hook, hook_rules

Q = "\x01"  # stands in for a quoted or substituted part inside a word
SEPS = ("&&", "||", "|", ";", "&")
REDIR = re.compile(r"(?:&>>?|>>|>\||>&|<&|<>|<<<|>|<)")
NAME = re.compile(r"[A-Za-z_][\w.+-]{0,39}")
SUB = re.compile(r"[a-z][a-z0-9-]{0,23}")
DIGITS4 = re.compile(r"\d{4}")
NUMBER = re.compile(r"-?\d+(?:[.,]\d+)?[A-Za-z%]{0,2}")
ID = re.compile(r"(?=[0-9a-fA-F]*[a-fA-F])[0-9a-fA-F]{7,}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
FLAG = re.compile(r"(--?[A-Za-z][\w.-]{0,30})(=.*)?", re.S)
ASSIGN = re.compile(r"([A-Za-z_]\w{0,63})=", re.S)
URL = re.compile(r"[A-Za-z][\w+.-]*://([^/?#\s]*)")
HOST = re.compile(r"[a-z0-9][a-z0-9.-]{0,252}")


def shape(command, prefixes=None):
    """The command's shape, or None for empty or unparseable input (unbalanced quotes or substitutions).

    prefixes: wrapper words to strip at command position; None reads the command-prefixes seam."""
    if prefixes is None:
        prefixes = core.seam_lines("command-prefixes")
    items = _tokens(command)
    if items is None:
        return None
    segs, seps = [[]], []
    for it in items:
        if it[0] == "sep":
            segs.append([])
            seps.append(it[1])
        else:
            segs[-1].append(it)
    cmd = _prefix_re(tuple(prefixes))
    pairs = [(_strip(s, cmd), seps[i] if i < len(seps) else None) for i, s in enumerate(segs)]
    pairs = [(s, sep) for s, sep in pairs if s]
    while len(pairs) > 1 and _word(pairs[0][0][0]) == "cd" and pairs[0][1] in ("&&", ";"):
        pairs.pop(0)
    if not pairs:
        return None
    out = []
    for i, (seg, sep) in enumerate(pairs):
        out.append(_segment(seg))
        if i < len(pairs) - 1:
            out.append("; " if sep == ";" else f" {sep} ")
    return "".join(out)


@functools.lru_cache(maxsize=None)
def _prefix_re(prefixes):
    """cmd_re with the longest wrapper first, so `wrapx raw` is taken whole, not as `wrapx`."""
    return re.compile(hook_rules.cmd_re(sorted(prefixes, key=lambda p: -len(p.split()))))


def _word(it):
    return it[1] if it[0] == "word" else None


def _strip(seg, cmd):
    """seg without its leading keywords, VAR=x and prefix words (repeated: `wrapx X=1 timeout 5 cmd`).
    A segment of assignments only (`X=1`) is kept whole."""
    seg = [it for it in seg if _word(it) != "}"]
    rest = seg
    while rest:
        rep = " ".join(it[1].replace(" ", "_") if it[0] == "word" else "\x02" for it in rest) + " "
        n = len(rep[:re.match(cmd, rep).end()].split())
        if not n:
            break
        rest = rest[n:]
    return rest or seg


def _segment(seg):
    """One simple command's shape: command word, up to one subcommand word, then placeholders."""
    out, after_flag = [], False
    for i, it in enumerate(seg):
        kind, text = it
        if kind == "heredoc":
            piece = "<heredoc>"
        elif kind == "redir":
            piece = text
        elif i == 0 and NAME.fullmatch(text) and not DIGITS4.search(text):
            piece = text
        elif i == 1 and seg[0][0] == "word" and out[0] == seg[0][1] and SUB.fullmatch(text) \
                and not DIGITS4.search(text) and not ID.fullmatch(text):
            piece = text
        else:
            piece = _value(text, after_flag)
        after_flag = kind == "word" and piece.startswith("-") and piece not in ("-", "--") and "=" not in piece
        if not (out and piece == out[-1] and piece.startswith("<") and piece != "<heredoc>"):
            out.append(piece)
    return " ".join(out)


def _value(t, after_flag):
    """The placeholder for one word outside the command/subcommand slots (flag names are kept)."""
    m = ASSIGN.match(t)
    if m and not DIGITS4.search(m[1]):
        return f"{m[1]}=<v>"
    m = FLAG.fullmatch(t)
    if m and not DIGITS4.search(m[1]):
        return m[1] + ("=<v>" if m[2] is not None else "")
    if Q in t:
        return "<str>"
    if t in ("-", "--"):
        return t
    if NUMBER.fullmatch(t):
        return "<n>"
    if ID.fullmatch(t):
        return "<id>"
    m = URL.match(t)
    if m:
        host = m[1].rsplit("@", 1)[-1].split(":", 1)[0].lower()
        return f"<url:{host}>" if HOST.fullmatch(host) and not DIGITS4.search(host) else "<url>"
    if "/" in t or t.startswith("~") or t in (".", ".."):
        return "<path>"
    return "<v>" if after_flag else "<arg>"


def _tokens(s):
    """[(kind, text)] with kind word|sep|redir|heredoc; quoted and $( ) parts of a word become Q.
    None when a quote, backtick or substitution is unbalanced."""
    out, buf, pending, i = [], [], [], 0

    def end():
        if buf:
            out.append(("word", "".join(buf)))
            buf.clear()

    while i < len(s):
        c = s[i]
        if c == "\\":
            if s.startswith("\n", i + 1):
                end()
            else:
                buf.append(s[i + 1:i + 2])
            i += 2
        elif c == "'" or c == "`":
            j = s.find(c, i + 1)
            if j < 0:
                return None
            buf.append(Q)
            i = j + 1
        elif c == '"':
            _, i = hook._dquote(s, i + 1)
            if i > len(s):
                return None
            buf.append(Q)
        elif s.startswith("$(", i):
            _, j = hook._scan(s, i + 2, True)
            if j > len(s) or s[j - 1] != ")":
                return None
            buf.append(Q)
            i = j
        elif c == "#" and not buf and (i == 0 or s[i - 1] in hook.WORD_START):
            nl = s.find("\n", i)
            i = len(s) if nl < 0 else nl
        elif s.startswith("<<", i) and not s.startswith("<<<", i) and hook.HEREDOC.match(s, i):
            end()
            m = hook.HEREDOC.match(s, i)
            pending.append((next(g for g in m.groups()[1:] if g is not None), m[1] == "-"))
            out.append(("heredoc", ""))
            i = m.end()
        elif c == "\n":
            end()
            out.append(("sep", ";"))
            i = hook._skip_heredocs(s, i + 1, pending) if pending else i + 1
            pending = []
        elif c in "<>" or s.startswith("&>", i):
            fd = "".join(buf) if buf and "".join(buf).isdigit() else ""
            if fd:
                buf.clear()
            end()
            op = REDIR.match(s, i)[0]
            i += len(op)
            if op.endswith("&"):
                t = re.match(r"\d+-?|-", s[i:])
                op += t[0] if t else ""
                i += len(t[0]) if t else 0
            out.append(("redir", fd + op))
        elif c in "&|;":
            end()
            op = next(p for p in SEPS if s.startswith(p, i))
            i += len(op) + (s.startswith("|&", i) or s.startswith(";;", i))
            out.append(("sep", op))
        elif c in " \t()":
            end()
            i += 1
        else:
            buf.append(c)
            i += 1
    end()
    return out if any(k == "word" for k, _ in out) else None
