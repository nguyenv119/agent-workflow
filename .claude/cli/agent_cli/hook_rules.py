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
WT_ADD = r"git\b(?:\s+-C\s+\S+)?\s+worktree\s+add\b"


def builtin(cmd):
    """The built-in rules, anchored with this hook call's CMD (seam prefixes included)."""
    return [
        {"id": "agent-cli-hand-verdict", "mode": "deny", "requires": "agent-cli", "needs": re.escape(SENTINEL),
         "match": cmd + r"(?:echo|printf)\b[^|;&)\n]*" + re.escape(SENTINEL),
         "message": "The eval prints WIN, not a worker. Use the verdict library: "
                    "source \"$(agent-cli eval lib --sh)\" and call win; run evals with agent-cli eval run <dir> "
                    "(coming in the eval bead)."},
        {"id": "hand-worktree-add", "mode": "deny", "requires": "agent-cli", "on": "code",
         "match": cmd + WT_ADD + r"[^;&|\n]*\s-[bB]",
         "message": "Use: agent-cli wt new <bead-id> <slug> --base origin/main  (it also runs the project's "
                    "setup hook)."},
        {"id": "hand-worktree-add-other", "mode": "warn", "requires": "agent-cli", "on": "code",
         "match": cmd + WT_ADD + r"(?![^;&|\n]*\s-[bB])", "unless": r"--detach\b",
         "message": "For an existing branch: agent-cli wt new <id> <slug> --existing <branch>"},
        {"id": "hand-sleep-poll", "mode": "warn", "requires": "agent-cli", "on": "code", "needs": r"\bsleep\s+\d",
         "match": cmd + r"(?:while|until)\b" + body_re(cmd) + cmd + r"sleep\s+(?:\d{2,}|[3-9])\b",
         "message": "Waiting on something? agent-cli wait --until '<status cmd>' --every 60 --cap 30m "
                    "(run_in_background) stops on success, passes through refused/blocked, exits 4 on timeout."},
    ]
