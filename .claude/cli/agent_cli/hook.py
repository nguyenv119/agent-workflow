"""PreToolUse hook framework: the command's code view, rule loading and evaluation, the `hook` verbs
(posttooluse/posttoolusefailure, the shape recorder, live in record.py).

Safety: fails open (any error prints nothing, exit 0); AGENT_CLI_HOOKS=off disables every rule;
`# agent-cli: allow <id>` skips one rule; only Bash is inspected; commands over 20 KB are not matched.
"""
import json
import os
import re
import shutil
import sys

from . import core, hook_rules

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


def evaluate(command, rules):
    """The rules that fire on command, in rule order (none when switched off or over MAX_CMD)."""
    if os.environ.get("AGENT_CLI_HOOKS") == "off" or len(command) > MAX_CMD:
        return []
    bypass, code = set(BYPASS.findall(command)), code_view(command)
    return [r for r in rules if r["id"] not in bypass and match(r, command, code) and active(r)]


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
    text = rule["message"]
    if not shutil.which("agent-cli"):
        text = re.sub(r"(?<![\w/.-])agent-cli(?![\w-])", str(core.BIN / "agent-cli"), text)
    return f"{text}  (bypass: append  # agent-cli: allow {rule['id']})"


def pretooluse():
    """Read the PreToolUse JSON on stdin; print a deny or warn envelope, or nothing. Never raises."""
    try:
        data = json.loads(sys.stdin.read())
        if data.get("tool_name") != "Bash":
            return
        hits = evaluate(data["tool_input"]["command"], load_rules())
        deny = [r for r in hits if r["mode"] == "deny"]
        out = {"hookEventName": "PreToolUse"}
        if deny:
            out.update(permissionDecision="deny", permissionDecisionReason=message(deny[0]))
        elif hits:
            out["additionalContext"] = "\n".join(message(r) for r in hits)
        else:
            return
        print(json.dumps({"hookSpecificOutput": out}))
    except Exception:  # fail open: a broken hook must never block a session
        return


def test(command, json_mode):
    """Exit 2 when a rule denies command, else 0; prints the decision and the rules that fired."""
    hits = evaluate(command, load_rules())
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
    for r in load_rules():
        row = {"id": r["id"], "mode": r["mode"], "source": r["source"], "active": active(r),
               "requires": r.get("requires")}
        if json_mode:
            print(json.dumps(row))
        else:
            state = "active" if row["active"] else f"inactive (needs {row['requires']})"
            print(f"{row['id']}  {row['mode']}  {row['source']}  {state}")


def register(sub):
    _, hs = core.group(sub, "hook", "PreToolUse rules that point hand-rolled chores at the right command, and the shape recorder",
                       ["agent-cli hook test --cmd 'echo \"<promise>WIN</promise>\"'", "agent-cli hook rules --json",
                        "agent-cli hook scan .claude/loop-evals --since 7d --json"])
    p = core.subcommand(hs, "pretooluse",
                        "read a PreToolUse event on stdin; print a deny or warn envelope, or nothing",
                        ["echo '{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"ls\"}}' "
                         "| agent-cli hook pretooluse"])
    p.set_defaults(func=lambda a: pretooluse())
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
