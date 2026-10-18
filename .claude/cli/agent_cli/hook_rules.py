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
ASSIGNS = r"(?:[A-Za-z_]\w*=\S*[^\S\n]+)*"  # stops at a newline: each line is its own command position
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


def loop_poll_re(cmd, loops, poll):
    """A `loops` loop that both sleeps and runs `poll` (either order; the poll may be the condition, and
    the sleep may open it: `while sleep 30; do gh pr checks 12 && break; done`)."""
    loop, body, sleep = cmd + r"(?:" + loops + r")\b", body_re(cmd), cmd + SLEEP
    return (r"(?:" + loop + body + sleep + body + poll + "|" + loop + body + poll + body + sleep
            + "|" + loop + r"\s+(?:!\s+)?" + SLEEP + body + poll + ")")


def pr_poll_re(cmd):
    """A while/until loop that both sleeps and polls a PR, or `gh pr checks --watch`.
    `for` over a fixed list is not polling."""
    return (r"(?:" + loop_poll_re(cmd, "while|until", PR_POLL)
            + "|" + cmd + r"gh\s+pr\s+checks\b[^;&|\n]*--watch)")


PR_POLL = r"gh\s+(?:pr\s+(?:checks|view)|api\s+\S*pulls)"
COMMIT_POLL = r"gh\s+api\b[^;&|\n]*?commits/[^\s/]+/(?:status(?:es)?|check-runs|check-suites)\b"  # a commit's CI
COUNTED_FOR = r"for\s*\(\(|for\s+\w+\s+in\s+(?:\"?\$\(\s*seq\b|\{-?\d+\.\.)"  # $(seq ..), {1..n}, ((;;))
SLEEP = r"sleep\s+\d"


def not_in_loop(cmd):
    """Lookahead: no `done` at command position follows before a new loop opens, so the match is not in a
    loop's body or condition. Part of `match` because `hook scan` checks `unless` only to the end of the
    match's line, which misses the loop around a multi-line body."""
    return r"(?!(?:(?!" + cmd + r"(?:while|until|for)\b)[\s\S]){0,2000}?" + cmd + r"done\b)"


def builtin(cmd):
    """The built-in rules, anchored with this hook call's CMD (seam prefixes included)."""
    return [
        {"id": "hand-pr-poll", "mode": "deny", "requires": "agent-cli", "on": "code",
         "needs": r"gh\s+(?:pr|api)\b", "match": pr_poll_re(cmd),
         "message": "Use: agent-cli pr wait <n> --cap 40m  (run_in_background; exit 0 green, 1 red, 4 timeout). "
                    "One-shot: agent-cli pr status <n>."},
        {"id": "agent-cli-hand-verdict", "mode": "deny", "requires": "agent-cli", "needs": re.escape(SENTINEL),
         "match": cmd + r"(?:echo|printf)\b[^|;&)\n]*" + re.escape(SENTINEL),
         "message": "The eval prints WIN, not a worker. Run it: agent-cli eval run <dir> (exit 0 = WIN). "
                    "Inside an eval: source \"$(agent-cli eval lib --sh)\" and call win."},
        {"id": "hand-worktree-add", "mode": "deny", "requires": "agent-cli", "on": "code",
         "match": cmd + WT_ADD + r"[^;&|\n]*\s-[bB]",
         "message": "Use: agent-cli wt new <bead-id> <slug> --base origin/main  (it also runs the project's "
                    "setup hook)."},
        {"id": "hand-worktree-add-other", "mode": "warn", "requires": "agent-cli", "on": "code",
         "match": cmd + WT_ADD + r"(?![^;&|\n]*\s-[bB])", "unless": r"--detach\b",
         "message": "For an existing branch: agent-cli wt new <id> <slug> --existing <branch>"},
        {"id": "hand-sleep-poll", "mode": "warn", "requires": "agent-cli", "on": "code", "needs": r"\bsleep\s+\d",
         "match": (cmd + r"(?:while\b|until\b|" + COUNTED_FOR + ")" + body_re(cmd) + cmd
                   + r"sleep\s+(?:\d{2,}|[3-9])\b"  # a while/until/counted-for loop sleeping 3s+
                   + "|" + loop_poll_re(cmd, "while|until|for", COMMIT_POLL)),  # any loop polling commit CI
         "unless": pr_poll_re(cmd),  # a PR poll loop is hand-pr-poll's deny, not this warn
         "message": "Waiting on something? agent-cli wait --until '<status cmd>' --every 60 --cap 30m "
                    "(run_in_background) stops on success, passes through refused/blocked, exits 4 on timeout."},
        {"id": "piped-wait", "mode": "warn", "requires": "agent-cli", "on": "code", "needs": r"\bwait\b",
         "match": cmd + r"(?:\S*/)?agent-cli\s+(?:pr\s+)?wait\b(?!-)(?:[<>]&|[^;&|\n])*\|(?!\|)",  # | or |&, never ||
         "unless": r"--help\b|\bpipefail\b|\bPIPESTATUS\b",  # these keep the wait's exit
         "message": "Don't pipe agent-cli wait: the pipe's exit is the last command's, so a timeout (exit 4) or "
                    "a failure (exit 3) reads as 0. Run it unpiped with run_in_background; add --json for one "
                    "line per attempt."},
        {"id": "hand-loop-log", "mode": "warn", "requires": "agent-cli", "on": "raw", "needs": r"\bloop-",
         "match": cmd + r"(?:echo|printf)\b[^|;&\n]*>>\s*[\"']?\S*\bloop-[^\s\"'/]*\.(?:log|txt)\b",
         "message": "Loop logs: agent-cli log append .claude/loop-<epic-id>.tsv --phase ... --action ... --result ... "
                    "(one TSV row, escaped, safe under concurrency)."},
        {"id": "hand-pr-status", "mode": "warn", "requires": "agent-cli", "on": "code", "needs": r"gh\s+pr\b",
         "match": cmd + r"gh\s+pr\s+(?:view\b[^;&|\n]*--json|checks\b)" + not_in_loop(cmd),
         "unless": pr_poll_re(cmd),  # a poll loop or gh pr checks --watch is hand-pr-poll's deny
         "message": "One-shot PR state and checks: agent-cli pr status <n> (--json for fields)."},
        {"id": "hand-worktree-list-rm", "mode": "warn", "requires": "agent-cli", "on": "code", "needs": "worktree",
         "match": cmd + r"git\b(?:\s+-C\s+\S+)?\s+worktree\s+(?:list|remove)\b",
         "message": "agent-cli wt list (branch, bead, dirty, ahead/behind) / agent-cli wt rm --path <path> "
                    "(dry run unless --yes)."},
        {"id": "hand-bare-sleep", "mode": "warn", "requires": "agent-cli", "on": "code", "needs": r"\bsleep\s+\d",
         "match": cmd + r"sleep\s+(?:(?:[2-9]\d|[1-9]\d{2,})s?|[1-9]\d*[mh])\b" + not_in_loop(cmd),  # >= 20s
         "unless": cmd + r"(?:while|until|for)\b",  # any loop in the command: hand-sleep-poll's ground
         "message": "Waiting on something? agent-cli wait --until '<status cmd>' --every 60 --cap 30m "
                    "(run_in_background)."},
    ]
