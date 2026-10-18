"""PreToolUse hook framework: the command's code view, rule loading and evaluation, the repeated-shape
warning, the UserPromptSubmit phrase hook and the `hook` verbs (posttooluse/posttoolusefailure, the shape
recorder, live in record.py).

Safety: fails open (any error prints nothing, exit 0); AGENT_CLI_HOOKS=off disables every rule and both
warnings; `# agent-cli: allow <id>` skips one rule (`repeated-shape` for the repeat warning); Bash commands meet
every rule, Edit/Write/MultiEdit/NotebookEdit only harness-edit-on-main (harness_edit.py); commands over
20 KB are not matched.
"""
import functools
import json
import os
import re
import shutil
import subprocess
import sys

from . import core, harness_edit, hook_rules

MAX_CMD = 20 * 1024
BYPASS = re.compile(r"#\s*agent-cli:\s*allow\s+([\w.-]+)")
SENTINEL_STR = re.compile(re.escape(hook_rules.SENTINEL) + r"(?:\\n)?")
WORD_START = " \t\n;&|()<>"
HEREDOC = re.compile(r"<<(-?)[ \t]*(?:'([^'\n]*)'|\"([^\"\n]*)\"|\\?([A-Za-z_][\w.-]*))")


def code_view(cmd, nls=None):
    """The command as rules see it by default: quoted strings -> "" (an exact sentinel string stays as
    the bare token; "$(...)" keeps each substitution, itself stripped), heredoc bodies and # comments removed.
    nls: a list that gets, in order, the index in cmd of the newline ending each view line (for a heredoc,
    the end of its body), which maps view lines back to cmd lines."""
    return _scan(cmd, 0, False, nls)[0]


def _scan(s, i, in_subst, nls=None):
    """Scan from i; in a $( ) substitution stop after its closing paren. Returns (text, next index)."""
    out, depth, pending = [], 0, []
    while i < len(s):
        c = s[i]
        if c == "\\":
            if nls is not None and s.startswith("\n", i + 1):
                nls.append(i + 1)
            out.append(s[i:i + 2])
            i += 2
        elif c == "'":
            end = s.find("'", i + 1)
            end = len(s) if end < 0 else end
            out.append(_literal(s[i + 1:end]))
            i = end + 1
        elif c == '"':
            text, i = _dquote(s, i + 1, nls)
            out.append(text)
        elif c == "#" and (i == 0 or s[i - 1] in WORD_START):
            nl = s.find("\n", i)
            i = len(s) if nl < 0 else nl
        elif c == "<" and s.startswith("<<", i) and not s.startswith("<<<", i) and HEREDOC.match(s, i):
            m = HEREDOC.match(s, i)
            delim = next(g for g in m.groups()[1:] if g is not None)
            pending.append((delim, m[1] == "-"))
            out.append("<<" + delim)
            i = m.end()
        elif c == "\n" and pending:
            out.append("\n")
            i = _skip_heredocs(s, i + 1, pending)
            pending = []
            if nls is not None:  # the next view line starts after the last heredoc body
                nls.append(i - 1)
        elif c == "(":
            depth += 1
            out.append(c)
            i += 1
        elif c == ")" and in_subst and depth == 0:
            return "".join(out), i + 1
        else:
            depth -= c == ")"
            if nls is not None and c == "\n":
                nls.append(i)
            out.append(c)
            i += 1
    return "".join(out), i


def _literal(text):
    """A quoted literal becomes "" unless it is exactly the sentinel (plus an optional literal \\n)."""
    return hook_rules.SENTINEL if SENTINEL_STR.fullmatch(text) else '""'


def _dquote(s, i, nls=None):
    """A double-quoted string from i (just past the quote): keep stripped $( ) substitutions, drop the rest."""
    lit, subs = [], []
    while i < len(s) and s[i] != '"':
        if s[i] == "\\":
            lit.append(s[i:i + 2])
            i += 2
        elif s.startswith("$(", i):
            text, i = _scan(s, i + 2, True, nls)
            subs.append("$(" + text + ")")
        else:
            lit.append(s[i])
            i += 1
    if not subs and SENTINEL_STR.fullmatch("".join(lit)):
        return hook_rules.SENTINEL, i + 1
    return '"' + "".join(subs) + '"', i + 1


def _skip_heredocs(s, i, pending):
    """Skip each pending heredoc body (through its terminator line); returns the index after the last."""
    for delim, dash in pending:
        while i < len(s):
            nl = s.find("\n", i)
            end = len(s) if nl < 0 else nl
            line = s[i:end]
            i = end + 1
            if (line.lstrip("\t") if dash else line) == delim:
                break
    return min(i, len(s))


def load_rules():
    """Built-ins plus the project's seam rules. A file that is not a JSON array is ignored, and a bad rule
    is skipped alone, each with one stderr line naming the file (and the rule's id, or #index)."""
    cmd = hook_rules.cmd_re(core.seam_lines("command-prefixes"))
    rules = [dict(r, source="builtin") for r in hook_rules.builtin(cmd)]
    f = core.find_seam("pretooluse-rules.json")
    if not f:
        return rules
    try:
        project = json.loads(f.read_text())
        if not isinstance(project, list):
            raise ValueError("must be a JSON array of rules")
    except Exception as e:  # an unreadable file drops the project rules, never the hook
        sys.stderr.write(f"agent-cli hook: ignoring {f}: {e}\n")
        return rules
    body, ids = hook_rules.body_re(cmd), {r["id"] for r in rules}
    for i, r in enumerate(project):
        label = r["id"] if isinstance(r, dict) and isinstance(r.get("id"), str) else f"#{i}"
        try:
            rule = _project_rule(r, cmd, body, ids)
        except (ValueError, re.error) as e:
            sys.stderr.write(f"agent-cli hook: ignoring rule {label} in {f}: {e}\n")
            continue
        ids.add(rule["id"])
        rules.append(rule)
    return rules


def _project_rule(r, cmd, body, taken):
    """A validated copy of one project rule with {CMD}/{BODY} filled in; raises ValueError or re.error."""
    if not isinstance(r, dict):
        raise ValueError("must be a JSON object")
    if not isinstance(r.get("id"), str) or r["id"] in taken:
        raise ValueError("needs a string id not used by a built-in or an earlier rule")
    if not (r.get("mode") in ("deny", "warn") and isinstance(r.get("message"), str)
            and r.get("on", "code") in ("code", "raw") and isinstance(r.get("match"), str)):
        raise ValueError("needs match, mode deny|warn, message, on code|raw")
    for k in ("unless", "needs", "requires"):
        if k in r and not isinstance(r[k], str):
            raise ValueError(f"{k} must be a string")
    rule = dict(r, source="project")
    for k in ("match", "unless", "needs"):
        if k in rule:
            rule[k] = rule[k].replace("{CMD}", cmd).replace("{BODY}", body)
            re.compile(rule[k])
    return rule


def active(rule):
    """A rule with `requires` fires only when that command is on PATH or in the dirs `self env` adds."""
    req = rule.get("requires")
    return not req or bool(shutil.which(req) or shutil.which(req, path=os.pathsep.join(core.path_dirs())))


def evaluate(command, rules, honor_bypass=True):
    """The rules that fire on command, in rule order (none when switched off or over MAX_CMD).
    honor_bypass=False also returns the rules a `# agent-cli: allow <id>` comment skips."""
    if os.environ.get("AGENT_CLI_HOOKS") == "off" or len(command) > MAX_CMD:
        return []
    bypass, code = set(BYPASS.findall(command)) if honor_bypass else set(), code_view(command)
    return [r for r in rules if r["id"] not in bypass and match(r, command, code) and active(r)]


def check(command, cwd):
    """evaluate() with every rule, then, when nothing denies, harness-edit-on-main on the files the
    command writes (relative paths resolved against cwd)."""
    hits = evaluate(command, load_rules())
    if (not any(r["mode"] == "deny" for r in hits) and os.environ.get("AGENT_CLI_HOOKS") != "off"
            and len(command) <= MAX_CMD and harness_edit.RULE_ID not in BYPASS.findall(command)):
        try:
            written, removed = harness_edit.bash_targets(command)
            w = harness_edit.first(written, cwd, removed)
        except (OSError, subprocess.TimeoutExpired):  # git missing or slow: fail open, as pretooluse does
            w = None
        hits += [harness_edit.rule(w)] if w else []
    return hits


def match(rule, raw, code):
    """The rule's match object in its view (code, or raw when on == "raw"), or None when its `needs`
    misses raw, `match` misses, or `unless` hits. Ignores `requires` and bypass comments."""
    text = raw if rule.get("on") == "raw" else code
    if "needs" in rule and not re.search(rule["needs"], raw):
        return None
    m = re.search(rule["match"], text)
    return None if not m or ("unless" in rule and re.search(rule["unless"], text)) else m


def message(rule):
    """The rule's message with the bypass hint; bare `agent-cli` becomes the absolute shim path when it is
    not on PATH (the PATH wiring failed)."""
    return f"{_shim(rule['message'])}  (bypass: append  # agent-cli: allow {rule['id']})"


def _shim(text):
    """text with bare `agent-cli` as the absolute shim path when agent-cli is not on PATH."""
    if not shutil.which("agent-cli"):
        text = re.sub(r"(?<![\w/.-])agent-cli(?![\w-])", str(core.BIN / "agent-cli"), text)
    return text


def pretooluse():
    """Read the PreToolUse JSON on stdin; print a deny or warn envelope, or nothing. Never raises.
    With no rule firing, a shape on the repeats cache adds the repeated-shape warning (_repeat_rule)."""
    try:
        data = json.loads(sys.stdin.read())
        tool, cwd = data.get("tool_name"), data.get("cwd") or os.getcwd()
        if tool in harness_edit.EDIT_TOOLS:
            path = data["tool_input"].get(harness_edit.EDIT_TOOLS[tool])
            w = os.environ.get("AGENT_CLI_HOOKS") != "off" and isinstance(path, str) and harness_edit.first([path], cwd)
            if w:
                _deny(_shim(harness_edit.rule(w)["message"]) + "  (bypass: AGENT_CLI_HOOKS=off)")
            return
        if tool != "Bash":
            return
        command = data["tool_input"]["command"]
        hits = check(command, cwd)
        deny = [r for r in hits if r["mode"] == "deny"]
        if deny:
            return _deny(message(deny[0]))
        # a rule that fires already names the command for this chore, so the repeat warning is only
        # for commands no rule matches
        hits = hits or [r for r in [_repeat_rule(command, data)] if r]
        if hits:
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                                     "additionalContext": "\n".join(message(r) for r in hits)}}))
    except Exception:  # fail open: a broken hook must never block a session
        return


def _deny(reason):
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                             "permissionDecisionReason": reason}}))


REPEAT_ID = "repeated-shape"
# Generic reads: shapes made only of these top the repeats list (an agent reading files), but there is no
# chore in them for a command to wrap, so they never warn (basic() counts them as basic) and the step
# report drops them; --unit command keeps them. Transforms (sort, uniq, cut, tr, awk, jq) are left out on
# purpose: `git log ... | sort | uniq -c` is a recipe a command can wrap, not a look at a file.
QUIET_WORDS = frozenset("""cat head tail less more sed grep egrep rg ls tree find wc diff stat file du df echo
printf pwd cd read which type true sleep date basename dirname realpath readlink""".split())
QUIET_GIT = frozenset("status diff log show".split())
INLINE_CODE = frozenset(("-", "-c", "-e", "<heredoc>"))  # `python3 - <heredoc>`: the code is the value
SEGMENT_SEP = re.compile(r" (?:&&|\|\||\||&) |; ")
SESSION_CHARS = re.compile(r"[^\w-]")


def quiet(s):
    """True when every segment of shape s is a generic read: a QUIET_WORDS command, a git read, or an
    interpreter running inline code (`python3 - <heredoc>`, `node -e <str>`); a named script is loud."""
    from .shape import INTERPRETER
    for seg in SEGMENT_SEP.split(s):
        words = seg.split()
        if not words:
            continue
        name, second = words[0].rsplit("/", 1)[-1], words[1] if len(words) > 1 else ""
        if not (name in QUIET_WORDS and "/" not in words[0] or name == "git" and second in QUIET_GIT
                or INTERPRETER.fullmatch(name) and second in INLINE_CODE):
            return False
    return True


# Shell basics: no chore in them for a command to wrap. The step report drops basic steps (report_only
# ones aside), and the warning and its cache never use any. seq: `for i in $(seq 1 40)` loop counters.
BASIC_WORDS = frozenset("""export set unset mkdir cp mv rm rmdir ln chmod chown touch cd pwd true false exit read
printf echo test [ [[ sleep seq""".split())
BASIC_GIT = frozenset("add commit checkout switch restore branch fetch pull push stash rev-parse status diff log "
                      "show".split())
ASSIGN_WORD = re.compile(r"[A-Za-z_]\w*=")
INTERP_FLAG = re.compile(r"-[A-Za-z]{1,3}")  # -I, -u, -B before the inline code
PLACEHOLDER = re.compile(r"<[^<>]*>")  # a masked word: <arg>, <str>, <url:host>
CUT_CHARS = re.compile(r"-[cb](?:<v>)?|--(?:characters|bytes)(?:=<v>)?")  # cut -c<v>: truncation only


def _words(seg):
    """A step segment's words from its command word: past a leading shell keyword and VAR=<v> words, and
    for git past its global options (`git -C <arg> add <arg>` -> [git, add, <arg>])."""
    from .shape import GIT_GLOBAL, LEAD_KEYWORDS
    words = seg.split()
    if words and words[0] in LEAD_KEYWORDS:
        words = words[1:]
    while words and ASSIGN_WORD.match(words[0]):
        words = words[1:]
    if words[:1] == ["git"]:
        i = 1
        while i < len(words) and (words[i] in GIT_GLOBAL or words[i].split("=", 1)[0] in ("--git-dir", "--work-tree")):
            i += 1 + GIT_GLOBAL.get(words[i], False)
        words = ["git"] + words[i:]
    return words


@functools.lru_cache(maxsize=None)
def basic(step):
    """True when every `|` segment of a step is a shell basic: a BASIC_WORDS or QUIET_WORDS command, by name
    or by path (`<path>/grep`), `cut` with only -c/-b (truncation), a command made only of placeholders
    (`<arg>`: no literal word to wrap), git housekeeping or a git read (BASIC_GIT), or an interpreter
    running inline code after short flags (`python3 -I - <heredoc>`, `node -e <str>`).
    `echo <str> | psql <arg>`, `cut -d<v> -f<v>` and `<str> --headless=<v>` are not basic."""
    from .shape import INTERPRETER
    for seg in SEGMENT_SEP.split(step):
        words = _words(seg)
        if all(PLACEHOLDER.fullmatch(w) for w in words):  # assignments only, or no literal word
            continue
        name = words[0].rsplit("/", 1)[-1]
        if name == "git":
            ok = len(words) > 1 and words[1] in BASIC_GIT
        elif INTERPRETER.fullmatch(name):
            i = 1
            while i < len(words) and INTERP_FLAG.fullmatch(words[i]) and words[i] not in INLINE_CODE:
                i += 1
            ok = i < len(words) and words[i] in INLINE_CODE
        elif name == "cut":
            flags = [w for w in words[1:] if not PLACEHOLDER.fullmatch(w)]
            ok = bool(flags) and all(CUT_CHARS.fullmatch(w) for w in flags)
        else:
            ok = name in BASIC_WORDS or name in QUIET_WORDS
        if not ok:
            return False
    return True


@functools.lru_cache(maxsize=None)
def report_only(step):
    """True for a basic step the step report still lists, as a chore a command can do: a lone sleep
    (`sleep <n>`), or git log/show naming origin/<arg> (an on-main check, maybe piped into reads)."""
    if not basic(step):
        return False
    segs = SEGMENT_SEP.split(step)
    words = _words(segs[0])
    if words[:1] == ["sleep"]:
        return len(segs) == 1
    return words[:1] == ["git"] and words[1:2] in (["log"], ["show"]) \
        and any(w.startswith("origin/") or "..origin/" in w for w in words[2:])


def _repeat_rule(command, data):
    """The repeated-shape warning as a warn rule, the first time this session (and agent) types a step on
    <store>/repeats-cache.json; None otherwise. The cache is keyed by step (shape.steps_of_shape), so the
    command's whole shape is split: one step is looked up; two or more give None for now (picking which
    step of a chain to name is a later change). A basic step, no session id, a bypass comment, a missing or
    bad cache, or a store the once-only mark cannot be written to all give None: never warn twice. The cache
    is shared by every project on the store, so a step this project covers by a command (its `commands:`
    line, or agent-cli) is dropped here too."""
    try:
        from . import record, repeats, shape
        session = SESSION_CHARS.sub("_", str(data.get("session_id") or ""))[:128]
        if (not session or os.environ.get("AGENT_CLI_HOOKS") == "off" or len(command) > MAX_CMD
                or REPEAT_ID in BYPASS.findall(command)):
            return None
        steps = shape.steps_of_shape((shape.shape(command) or "")[:record.MAX_SHAPE])  # cut as the recorder does
        if len(steps) != 1:  # ponytail: one step only until the multi-step lookup lands
            return None
        s = steps[0]
        if basic(s) or repeats.command_covered()(s):
            return None
        store = record.store_dir()
        hit = json.loads((store / repeats.CACHE).read_text()).get(s)
        if hit is None:
            return None
        count, sessions = int(hit["count"]), int(hit["sessions"])
        mark = json.dumps([str(data.get("agent_id") or ""), s])
        warned = store / "warned"
        f = warned / f"{session}.txt"
        if f.is_file() and mark in f.read_text(encoding="utf-8", errors="replace").splitlines():
            return None
        warned.mkdir(mode=0o700, exist_ok=True)
        record.append_line(f, mark)
    except Exception:  # a missing or bad cache, or an unwritable store: no warning, and the rules still run
        return None
    return {"id": REPEAT_ID, "mode": "warn",
            "message": f"`{s}` was hand-written {count} times across {sessions} sessions; propose a command "
                       "(cli-for-agents skill: .claude/skills/cli-for-agents/SKILL.md and house-rules.md) "
                       "instead of typing it again; agent-cli repeats --top 10 lists the others."}


CLI_PHRASES = (r"\bshould\s+be\s+an?\s+(?:cli|command)\b",
               r"\bmake\s+(?:this|that|it)\s+(?:an?|into\s+an?)\s+(?:cli|command)\b",
               r"\bturn\s+(?:this|that|it)\s+into\s+an?\s+(?:cli|command)\b",
               r"\bwe\s+keep\s+(?:typing|doing)\s+this\b")
CLI_CONTEXT = ("The user wants this to become a command. File a bead for it whose acceptance cites "
               ".claude/skills/cli-for-agents/SKILL.md and house-rules.md; run agent-cli repeats --top 10 to see "
               "the shape counts.")
# Turns the harness starts arrive as UserPromptSubmit too, and the event carries no origin field (the
# transcript's turnOrigin may not be flushed yet), so the prompt's opening text tells them apart.
NOT_USER = re.compile(r"\s*(?:<system-reminder>(?:(?!</system-reminder>).)*</system-reminder>\s*)*"
                      r"(?:<task-notification>|<scheduled-task[\s>]|<ci-monitor-event>|\[SYSTEM NOTIFICATION"
                      r"|\[Cross-session idle notice\]|Stop hook feedback:|Another Claude session sent a message"
                      r"|This session is being continued from a previous)", re.S)


def userpromptsubmit():
    """Read the UserPromptSubmit JSON on stdin; when the prompt asks for a command (CLI_PHRASES, or the regex
    on the `cli-phrases:` line under `## repeats` in project-rules.md), print the file-a-CLI-bead context.
    Prints nothing otherwise, or when the prompt opens with a NOT_USER marker; never raises."""
    try:
        prompt = json.loads(sys.stdin.read())["prompt"]
        if os.environ.get("AGENT_CLI_HOOKS") == "off" or not isinstance(prompt, str) or NOT_USER.match(prompt):
            return
        patterns = list(CLI_PHRASES)
        extra = core.rules_line("repeats", "cli-phrases")
        if extra:
            try:
                re.compile(extra)
                patterns.append(extra)
            except re.error as e:
                sys.stderr.write(f"agent-cli hook: ignoring the cli-phrases regex in project-rules.md: {e}\n")
        if any(re.search(p, prompt, re.I) for p in patterns):
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                                     "additionalContext": _shim(CLI_CONTEXT)}}))
    except Exception:  # fail open: a broken hook must never block a prompt
        return


def test(command, json_mode):
    """Exit 2 when a rule denies command, else 0; prints the decision and the rules that fired."""
    hits = check(command, os.getcwd())
    decision = "deny" if any(r["mode"] == "deny" for r in hits) else "warn" if hits else "allow"
    rows = [{"id": r["id"], "mode": r["mode"], "message": message(r)} for r in hits]
    if json_mode:
        print(json.dumps({"decision": decision, "rules": rows}))
    else:
        print(f"decision: {decision}")
        for r in rows:
            print(f"{r['mode']}: {r['id']}: {r['message']}")
    return core.Exit.REFUSED if decision == "deny" else core.Exit.OK


def list_rules(json_mode):
    # harness-edit-on-main has no regex and reads branch state, so it lives outside load_rules(), which
    # hook scan, record and repeats call with no cwd and must not spawn git; it is listed by hand here
    for r in load_rules() + [harness_edit.rule()]:
        row = {"id": r["id"], "mode": r["mode"], "source": r["source"], "active": active(r),
               "requires": r.get("requires")}
        if json_mode:
            print(json.dumps(row))
        else:
            state = "active" if row["active"] else f"inactive (needs {row['requires']})"
            print(f"{row['id']}  {row['mode']}  {row['source']}  {state}")


def register(sub):
    _, hs = core.group(sub, "hook", "PreToolUse rules that point hand-rolled chores at the right command, the repeated-shape "
                       "warning, the prompt phrase hook and the shape recorder",
                       ["agent-cli hook test --cmd 'echo \"<promise>WIN</promise>\"'", "agent-cli hook rules --json",
                        "agent-cli hook scan .claude/loop-evals --since 7d --json"])
    p = core.subcommand(hs, "pretooluse",
                        "read a PreToolUse event on stdin; print a deny or warn envelope, or nothing",
                        ["echo '{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"ls\"}}' "
                         "| agent-cli hook pretooluse"])
    p.set_defaults(func=lambda a: pretooluse())
    p = core.subcommand(hs, "userpromptsubmit",
                        "read a UserPromptSubmit event on stdin; when the prompt asks for a command, print "
                        "context telling the agent to file a CLI bead, else nothing",
                        ["echo '{\"prompt\":\"this should be a CLI\"}' | agent-cli hook userpromptsubmit"])
    p.set_defaults(func=lambda a: userpromptsubmit())
    p = core.subcommand(hs, "test", "show which rules fire on a command (exit 2 when one denies)",
                        ["agent-cli hook test --cmd 'echo \"<promise>WIN</promise>\"'",
                         "agent-cli hook test --cmd 'gh pr checks 12' --json"])
    p.add_argument("--cmd", required=True, help="the Bash command to check")
    p.add_argument("--json", action="store_true", help="one JSON object: decision and rules")
    p.set_defaults(func=lambda a: test(a.cmd, a.json))
    p = core.subcommand(hs, "rules", "list built-in and project rules and whether each is active",
                        ["agent-cli hook rules", "agent-cli hook rules --json"])
    p.add_argument("--json", action="store_true", help="one JSON object per rule")
    p.set_defaults(func=lambda a: list_rules(a.json))
    from . import hook_scan, record  # both import this module, so registered here rather than at the top
    hook_scan.register(hs)
    for name, event in (("posttooluse", "PostToolUse"), ("posttoolusefailure", "PostToolUseFailure")):
        p = core.subcommand(hs, name, f"read a {event} event on stdin; record the command's shape (never the "
                            "command); prints nothing, always exits 0",
                            ["echo '{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"ls\"}}' "
                             f"| agent-cli hook {name}"])
        p.set_defaults(func=lambda a, event=event: record.hook_verb(event))
