"""A Bash command's shape: command and subcommand names and flag names kept, every value masked.

`gh pr view 12 --repo a/b --json state` -> `gh pr view <n> --repo <path> --json <v>`: quoted text ->
<str>, heredoc bodies -> <heredoc>, numbers -> <n>, hex ids and UUIDs -> <id>, anything with / -> <path>,
URLs -> <url:host> (<url> when the host looks like a value), X=value -> X=<v>, a flag's value -> <v>,
other bare words -> <arg>; env files keep their basename (`<path>/.env.local`) and remote refs their
remote (`origin/<arg>`). A command run by path keeps its name (`<path>/deploy.sh`), and so does the
script an interpreter runs (`python3 <path>/loop.py`); `.`, `source`, `[` and `[[` keep theirs, and so
do `+a`-style flags (`set +a`). The second word is kept only after a tool known to take subcommands
(SUBCOMMAND_TOOLS, the subcommand-tools seam's lines, or a name ending in -cli), after git's global
options; a third only after a two-level group (THIRD: `gh pr view`, `git worktree add`, `agent-cli wt
list`); package runners keep a script name with a `:` (COLON_TOOLS, e.g. pnpm/yarn/npm run test:unit);
a short flag with a value glued on keeps its letter only (`-p<v>`). Leading `cd <x> &&` chains and the prefixes
hook_rules.cmd_re knows (env, timeout N, VAR=x and the project's command-prefixes seam words) are
stripped. Env-file basenames, script names and subcommand words are kept on purpose, as names; flag
values, quoted text, numbers, ids, paths and hosts (other than URL hosts) never are, so it is safe to store.

steps() splits one command into its step shapes (each pipeline of a chain, loop body or $( ) body), so a
chore counts the same alone or as step 3 of a chain.
"""
import functools
import re

from . import core, hook, hook_rules

Q = "\x01"  # stands in for a quoted or substituted part inside a word
SEPS = ("&&", "||", "|", ";", "&")
REDIR = re.compile(r"(?:&>>?|>>|>\||>&|<&|<>|<<<|>|<)")
NAME = re.compile(r"[A-Za-z_][\w.+-]{0,39}")
SUB = re.compile(r"[a-z][a-z0-9-]{0,23}(?::[a-z][a-z0-9-]{0,23})*")  # `db:query` only after COLON_TOOLS
# Tools whose second word is a subcommand; after any other command the second word is a value.
SUBCOMMAND_TOOLS = frozenset("""git gh bd agent-cli docker kubectl helm npm npx yarn cargo go pip pip3 uv poetry
brew apt apt-get aws gcloud az terraform systemctl launchctl claude conda dolt hg svn rustup nix deno bun gem
bundle dotnet swift xcrun tmux openssl""".split()
                             + ["pnpm"])  # e.g. pnpm db:query
# Two-level groups: the word after (tool, group) is kept too; agent-cli keeps it after any group.
THIRD = {"gh": {"pr", "run", "api", "issue", "release", "workflow"},
         "git": {"worktree", "stash", "remote", "submodule"}, "npm": {"run"}, "yarn": {"run"},
         "pnpm": {"run"}}  # e.g. pnpm run test:unit
COLON_TOOLS = {("yarn",), ("npm", "run"), ("yarn", "run"),  # script names: test:unit
               ("pnpm",), ("pnpm", "run")}  # e.g. pnpm db:query
# git global options before the subcommand, and whether each takes the next word as its value.
GIT_GLOBAL = {"-C": True, "-c": True, "--git-dir": True, "--work-tree": True, "--no-pager": False}
ENV_FILE = re.compile(r"[\w.-]{0,40}\.env|\.env\.[\w.-]{1,40}")  # .env, prod.env, .env.local
PLUS_FLAGS = re.compile(r"\+[A-Za-z]|\+[a-z]{2,4}")  # `set +a`, the off switch of -a
# A `|` pipeline stays one step: `grep x f | sort` is one recipe. hook.basic()/report_only() split a step on
# | themselves (SEGMENT_SEP), to judge each segment.
STEP_SEP = re.compile(r" (?:&&|\|\||&) |; ")
REDIR_PIECE = re.compile(r"\d*" + REDIR.pattern + r"(&(?:\d+-?|-))?")  # group 1: `2>&1`, which has no target
KEYWORD_STEP = re.compile(r"done|fi|esac|\}|break|continue|(?:for|case|select) .*")
ASSIGN_PIECE = re.compile(r"[A-Za-z_]\w*=<v>")
LEAD_KEYWORDS = {"do", "then", "else", "elif", "if", "while", "until", "{", "!"}  # left on `do p=<v>`
RANGE_TO_REMOTE = re.compile(r"(.*?)(\.\.\.?)((?:origin|upstream)/.*)", re.S)  # `abc1234..origin/main`
MAX_BODY = 2000  # a longer $( ) body is an inline script, not a chore
# Interpreters whose script is the chore: `python3 <path>/loop.py`, not one shape for every script.
INTERPRETER = re.compile(r"(?:python[23]?(?:\.\d+)?|node|bash|sh|zsh|ruby|perl|tsx|ts-node)")
SCRIPT = re.compile(r"([A-Za-z_][\w.+-]{0,39})\.(?:py|sh|bash|zsh|js|mjs|cjs|ts|mts|rb|pl)")
SHORT_FLAGS = re.compile(r"-[A-Za-z]|-[a-z]{2,4}")  # -x, or a run like -la / -xvf; -pS3cret is not one
DIGITS4 = re.compile(r"\d{4}")
NUMBER = re.compile(r"-?\d+(?:[.,]\d+)?[A-Za-z%]{0,2}")
ID = re.compile(r"(?=[0-9a-fA-F]*[a-fA-F])[0-9a-fA-F]{7,}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
FLAG = re.compile(r"(--?[A-Za-z][\w.-]{0,30})(=.*)?", re.S)
ASSIGN = re.compile(r"([A-Za-z_]\w{0,63})=(?=[^=])", re.S)  # `abc==` (base64 padding) is not X=v
URL = re.compile(r"[A-Za-z][\w+.-]*://([^/?#\s]*)")
HOST = re.compile(r"[a-z0-9][a-z0-9.-]{0,252}")


def shape(command, prefixes=None, subcommands=None):
    """The command's shape, or None for empty or unparseable input (unbalanced quotes or substitutions).

    prefixes: wrapper words to strip at command position; None reads the command-prefixes seam.
    subcommands: tool names (beyond SUBCOMMAND_TOOLS) whose second word is kept; None reads the
    subcommand-tools seam."""
    return _shape(_tokens(command), prefixes, subcommands)


def steps(command, prefixes=None, subcommands=None):
    """The command's step shapes (steps_of_shape of its shape), then the steps of each $( ) or backtick
    body, one level deep, skipping bodies over MAX_BODY chars; [] when shape() is None.
    `TOKEN=$(grep KEY .env | cut -d= -f2)` -> [`grep <arg> .env | cut -d=<v> -f<v>`]."""
    if prefixes is None:  # read the seams once, not once per body
        prefixes = core.seam_lines("command-prefixes")
    if subcommands is None:
        subcommands = core.seam_lines("subcommand-tools")
    bodies = []
    s = _shape(_tokens(command, bodies), prefixes, subcommands)
    if s is None:  # bodies may hold a part of a command that failed to parse
        return []
    out = steps_of_shape(s)
    for body in bodies:
        if len(body) <= MAX_BODY:
            out += steps_of_shape(shape(body, prefixes, subcommands))
    return list(dict.fromkeys(out))


def steps_of_shape(s):
    """A shape string's steps: split on STEP_SEP, redirects dropped, keyword-only, loop-header-only and
    assignment-only steps dropped, whole-word <path> folded into <arg>, each cut to record.MAX_SHAPE,
    repeats kept once in first order. Works on stored shapes, so readers of old rows use it too; an old
    row keeps the masking it was recorded with, so its steps match new ones only where that masking did
    not change."""
    from .record import MAX_SHAPE  # record imports this module
    out = []
    for step in STEP_SEP.split(s or ""):
        words, skip = [], False
        for w in step.split(" "):
            m = None if skip else REDIR_PIECE.fullmatch(w)
            if skip or m:
                skip = bool(m and not m[1])  # a redirect without &N takes the next word as its target
                continue
            _push(words, "<arg>" if w == "<path>" else w)
        step = " ".join(words)
        rest = words[1:] if words and words[0] in LEAD_KEYWORDS else words
        if step and not KEYWORD_STEP.fullmatch(step) and not all(ASSIGN_PIECE.fullmatch(w) for w in rest):
            out.append(step[:MAX_SHAPE])
    return list(dict.fromkeys(out))


def _shape(items, prefixes, subcommands):
    if prefixes is None:
        prefixes = core.seam_lines("command-prefixes")
    tools = SUBCOMMAND_TOOLS.union(core.seam_lines("subcommand-tools") if subcommands is None else subcommands)
    if items is None:
        return None
    segs, seps = [[]], []
    for it in items:
        if it[0] == "sep":
            segs.append([])
            seps.append(it[1])
        else:
            segs[-1].append(it)
    cmd = prefix_re(tuple(prefixes))
    pairs = [(_strip(s, cmd), seps[i] if i < len(seps) else None) for i, s in enumerate(segs)]
    pairs = [(s, sep) for s, sep in pairs if s]
    while len(pairs) > 1 and _word(pairs[0][0][0]) == "cd" and pairs[0][1] in ("&&", ";"):
        pairs.pop(0)
    if not pairs:
        return None
    out = []
    for i, (seg, sep) in enumerate(pairs):
        out.append(_segment(seg, tools))
        if i < len(pairs) - 1:
            out.append("; " if sep == ";" else f" {sep} ")
    return "".join(out)


@functools.lru_cache(maxsize=None)
def prefix_re(prefixes):
    """cmd_re with the longest wrapper first, so `wrapx raw` is taken whole, not as `wrapx`."""
    return re.compile(hook_rules.cmd_re(sorted(prefixes, key=lambda p: -len(p.split()))))


def _word(it):
    return it[1] if it[0] == "word" else None


def _strip(seg, cmd):
    """seg without its leading keywords, VAR=x and prefix words (prefix_len). A segment of assignments only
    (`X=1`) is kept whole."""
    seg = [it for it in seg if _word(it) != "}"]
    return seg[prefix_len([it[1] if it[0] == "word" else "\x02" for it in seg], cmd):] or seg


def prefix_len(words, cmd):
    """How many of words (strings) come before the command word: keywords, VAR=x and prefix words, repeated
    (`wrapx X=1 timeout 5 cmd`), and env's own options (`env -u A gh`). Only env's: after `command -v` the
    next word is a value, never a command name to keep. cmd: prefix_re(...)."""
    k = 0
    while k < len(words):
        rep = " ".join(w.replace(" ", "_").replace("\n", "_") for w in words[k:k + 16]) + " "
        n = len(rep[:cmd.match(rep).end()].split())
        env = n and words[k + n - 1] == "env"
        while env and k + n < len(words) and words[k + n].startswith("-"):
            n += 1 + (words[k + n] in ("-u", "--unset", "-C", "--chdir", "-S"))
        if not n:
            break
        k += n
    return k


def _segment(seg, tools):
    """One simple command's shape: command word, up to two subcommand words, then placeholders."""
    out, after_flag, tool, script_slot = [], False, None, False
    slot, subs = 1, ()  # slot: index of the next word that may be a kept subcommand; subs: those kept
    for i, it in enumerate(seg):
        kind, text = it
        if kind == "heredoc":
            piece = "<heredoc>"
        elif kind == "redir":
            piece = text
        elif i == 0 and kind == "word" and (text in (".", "[", "[[") or _name(text.rsplit("/", 1)[-1])
                                            and not ASSIGN.match(text)):
            tool = text.rsplit("/", 1)[-1]
            piece = tool if "/" not in text else f"<path>/{tool}"
        elif i == slot and tool == "git" and not subs and kind == "word" \
                and (text in GIT_GLOBAL or text.split("=", 1)[0] in ("--git-dir", "--work-tree")):
            slot += 1 + GIT_GLOBAL.get(text, False)  # skip the option, and its value when it is the next word
            piece = _value(text, False)
        elif i == slot and tool and kind == "word" and _sub(text, (tool,) + subs in COLON_TOOLS) and (
                not subs and (tool in tools or tool.endswith("-cli"))
                or len(subs) == 1 and (subs[0] in THIRD.get(tool, ()) or tool == "agent-cli")):
            subs += (text,)
            slot += 1
            piece = text
        elif script_slot and kind == "word" and not text.startswith("-"):
            script_slot = False
            piece = _script(text) or _value(text, after_flag)
        else:
            piece = _value(text, after_flag)
        if i == 0 and tool and INTERPRETER.fullmatch(tool):
            script_slot = True  # the first word that is not a flag names the script (or is -c's code)
        after_flag = kind == "word" and piece.startswith("-") and piece not in ("-", "--") and "=" not in piece
        _push(out, piece)
    return " ".join(out)


def _push(words, w):
    """Append w unless it repeats the placeholder just before it (`<arg> <arg>` -> `<arg>`); heredocs
    are each kept."""
    if not (words and w == words[-1] and w.startswith("<") and w != "<heredoc>"):
        words.append(w)


def _script(t):
    """`<path>/name.py` or `name.py` for an interpreter's script argument, else None (an id-like or
    digit-laden name is a value, so it is masked as one)."""
    name = t.rsplit("/", 1)[-1]
    m = SCRIPT.fullmatch(name)
    if not m or not _name(m[1]) or Q in t:
        return None
    return f"<path>/{name}" if "/" in t else name


def _sub(t, colon):
    """A word that can be kept as a subcommand: lowercase, short, not an id or digit-laden; a `:` part
    (`db:query`) only when colon is set."""
    return bool(SUB.fullmatch(t)) and (colon or ":" not in t) and not DIGITS4.search(t) \
        and not any(ID.fullmatch(part) for part in t.split(":"))


def _name(t):
    """A word that can be kept as a command or script name: not an id or a digit-laden value
    (NAME starts with a letter, so numbers never match)."""
    return bool(NAME.fullmatch(t)) and not DIGITS4.search(t) and not ID.fullmatch(t)


def _value(t, after_flag):
    """The placeholder for one word outside the command/subcommand slots (flag names are kept)."""
    m = ASSIGN.match(t)
    if m and not DIGITS4.search(m[1]):
        return f"{m[1]}=<v>"
    m = FLAG.fullmatch(t)
    if m and not DIGITS4.search(m[1]):
        if not m[1].startswith("--") and not SHORT_FLAGS.fullmatch(m[1]):
            return m[1][:2] + "<v>"  # -pS3cret: a short flag with its value glued on
        return m[1] + ("=<v>" if m[2] is not None else "")
    if Q in t:
        return "<str>"
    if t in ("-", "--"):
        return t
    if NUMBER.fullmatch(t):
        return "<n>"
    if PLUS_FLAGS.fullmatch(t):
        return t
    if ID.fullmatch(t):
        return "<id>"
    m = URL.match(t)
    if m:
        host = m[1].rsplit("@", 1)[-1].split(":", 1)[0].lower()
        return f"<url:{host}>" if HOST.fullmatch(host) and not DIGITS4.search(host) else "<url>"
    if t.startswith(("origin/", "upstream/")):
        return t.split("/", 1)[0] + "/<arg>"  # `origin/main..`, `origin/main:a.ts`: a ref, not a path
    m = RANGE_TO_REMOTE.fullmatch(t)
    if m:
        return (_value(m[1], False) if m[1] else "") + m[2] + m[3].split("/", 1)[0] + "/<arg>"
    base = t.rsplit("/", 1)[-1]
    if ENV_FILE.fullmatch(base) and not DIGITS4.search(base):
        return f"<path>/{base}" if "/" in t else base
    if "/" in t or t.startswith("~") or t in (".", ".."):
        return "<path>"
    return "<v>" if after_flag else "<arg>"


def _tokens(s, bodies=None):
    """[(kind, text)] with kind word|sep|redir|heredoc; quoted and $( ) parts of a word become Q.
    None when a quote, backtick or substitution is unbalanced. bodies: a list that gets the text of
    each top-level $( ) and backtick body, inside double quotes too."""
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
            if c == "`" and bodies is not None:
                bodies.append(s[i + 1:j])
            buf.append(Q)
            i = j + 1
        elif c == '"':
            _, j = hook._dquote(s, i + 1)
            if j > len(s):
                return None
            if bodies is not None:
                _dquote_bodies(s, i + 1, j - 1, bodies)
            buf.append(Q)
            i = j
        elif s.startswith("$(", i):
            _, j = hook._scan(s, i + 2, True)
            if j > len(s) or s[j - 1] != ")":
                return None
            if bodies is not None and not s.startswith("$((", i):  # $(( )) is arithmetic, not a command
                bodies.append(s[i + 2:j - 1])
            buf.append(Q)
            i = j
        elif c == "#" and not buf and (i == 0 or s[i - 1] in hook.WORD_START):
            nl = s.find("\n", i)
            i = len(s) if nl < 0 else nl
        elif s.startswith("<<", i) and not s.startswith("<<<", i):
            if not hook.HEREDOC.match(s, i):  # e.g. <<$D: the body's end cannot be found, so its lines
                return None                   # would be read as commands
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


def _dquote_bodies(s, i, end, bodies):
    """Append each $( ) and backtick body in s[i:end], a double-quoted string's inside (as hook._dquote
    bounds it)."""
    while i < end:
        if s[i] == "\\":
            i += 2
        elif s.startswith("$(", i):
            _, j = hook._scan(s, i + 2, True)
            if j > end or s[j - 1] != ")":
                return
            if not s.startswith("$((", i):  # arithmetic, as in _tokens
                bodies.append(s[i + 2:j - 1])
            i = j
        elif s[i] == "`":
            j = s.find("`", i + 1)
            if j < 0 or j >= end:
                return
            bodies.append(s[i + 1:j])
            i = j + 1
        else:
            i += 1
