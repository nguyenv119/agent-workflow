"""Built-in PreToolUse rules and the regex pieces every rule anchors on.

A rule: {id, match, unless?, needs?, requires?, on?: "code"|"raw", mode: "deny"|"warn", message}.
`match`/`unless` are searched (no re.M) in the command's code view (quotes, heredoc bodies and comments
stripped) unless on == "raw"; `needs` is a cheap regex on the raw command checked first; a rule with
`requires` fires only when that command resolves. Project rules (seam pretooluse-rules.json) may write
{CMD} and {BODY} for the two pieces below.
Built-in rules use builtin()'s `cmd` argument (this call's CMD, seam prefixes included), never the
module-level CMD/BODY constants, which lack the prefixes.
"""
import re

KEYWORDS = r"(?:(?:do|then|else|elif|if|while|until|!|\{)\s+)*"
ASSIGNS = r"(?:[A-Za-z_]\w*=\S*\s+)*"
BASE_PREFIXES = ("env", "command", "time", "nohup", r"timeout\s+\S+")


def cmd_re(prefixes=()):
    """Command position: start, after ; & | ( or a newline, past shell keywords, VAR=x and prefix words.

    prefixes: extra wrapper words (lines of the command-prefixes seam); inner spaces match \\s+.
    """
    words = list(BASE_PREFIXES) + [r"\s+".join(map(re.escape, p.split())) for p in prefixes if p.split()]
    return r"(?:^\s*|[;&|(]\s*|\n\s*)" + KEYWORDS + ASSIGNS + r"(?:(?:" + "|".join(words) + r")\s+)*"


def body_re(cmd):
    """A bounded loop body that never crosses `done` at command position."""
    return r"(?:(?!" + cmd + r"done\b)[\s\S]){0,2000}?"


CMD = cmd_re()
BODY = body_re(CMD)
SENTINEL = "<promise>WIN</promise>"


def builtin(cmd):
    """The built-in rules, anchored with this hook call's CMD (seam prefixes included)."""
    return [
        {"id": "agent-cli-hand-verdict", "mode": "deny", "requires": "agent-cli", "needs": re.escape(SENTINEL),
         "match": cmd + r"(?:echo|printf)\b[^|;&)\n]*" + re.escape(SENTINEL),
         "message": "The eval prints WIN, not a worker. Use the verdict library: "
                    "source \"$(agent-cli eval lib --sh)\" and call win; run evals with agent-cli eval run <dir> "
                    "(coming in the eval bead)."},
        # Proves warn mode end to end; remove once the wait/lock bead ships its own warn rule.
        # Deliberately unanchored (no cmd prefix): acceptance runs `true agent-cli-warn-demo`, an argument.
        {"id": "agent-cli-demo-warn", "mode": "warn", "requires": "agent-cli",
         "match": r"\bagent-cli-warn-demo\b",
         "message": "agent-cli-demo-warn: demo warn rule fired; this text is hook context, the command still ran."},
    ]
