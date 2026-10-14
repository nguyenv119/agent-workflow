"""`agent-cli hook scan`: run every hook rule over script files (or piped commands) instead of a live command.

The question is "does this file hand-roll a chore a command now covers", so a rule's `requires` is ignored
and a `# agent-cli: allow <id>` comment does not hide a hit: it is reported with "bypassed": true.
Rules come from hook.load_rules() and see hook.code_view(), exactly as `hook pretooluse` does.
"""
import datetime
import fnmatch
import json
import os
import re
import sys
from bisect import bisect_right
from pathlib import Path

from . import core, hook, hook_rules
from .core import Exit

GLOB = "*.sh,*.py,*.ts,*.mjs"
SKIP_DIRS = {"node_modules", ".venv", "venv", ".git", "evidence"}
MAX_FILE = 1024 * 1024
LEAD = re.compile(r"[\s;&|(]*")
LOOP = re.compile(r"\b(?:while|until)\b")
DONE = re.compile(r"(?:^|[;&|(\n])\s*done\b")
EXAMPLES = ["agent-cli hook scan .claude/loop-evals --since 7d --json",
            "agent-cli hook scan scripts tools --glob '*.sh' --since 30d",
            "cat commands.txt | agent-cli hook scan --stdin --json",
            "agent-cli hook scan --since 7d --json   # the scan-dirs: line under ## gardener",
            "agent-cli hook scan .claude/loop-evals --from 2026-04-01 --until 2026-04-07 --json"]
WINDOW_EXAMPLE = EXAMPLES[4]
DESCRIPTION = ("Scans each file under the paths (mtime within --since, default 7d, or local time from --from\n"
               "(inclusive) up to --until; a bare date includes that whole day, a time is exclusive; skips\n"
               "node_modules, .venv, venv, .git, "
               "evidence/, binaries and files over 1 MB). With no paths it scans the comma-separated scan-dirs:\n"
               "line under `## gardener` in .claude/project-rules.md (relative to the project root).\n"
               "Prints one hit per file and rule {file, rule, mode, line, mtime, bypassed}, then a summary\n"
               "{files_scanned, unreadable, hits_by_rule, bypassed}. With --stdin, file is \"-\", line is the\n"
               "input line, mtime is null and the summary has commands_scanned (no files_scanned/unreadable).\n"
               "A bypass comment counts as a hit, not as compliance. A file that vanished or cannot be read is\n"
               "skipped and counted as unreadable. A hit after a multi-line quoted string may report the line\n"
               "the string starts on.\n"
               "Exit 0 whenever the scan ran, whatever it found; 1 on a scan error; 2 on bad usage.")


def register(hs):
    p = core.subcommand(hs, "scan", "run every rule over script files or piped commands (read-only)", EXAMPLES)
    p.description = DESCRIPTION
    p.add_argument("paths", nargs="*", help="files or dirs to scan (default: scan-dirs from project-rules.md)")
    p.add_argument("--since", help="only files modified within this long (default 7d; not with --from/--until)")
    p.add_argument("--from", dest="from_", metavar="WHEN",
                   help="only files modified on or after this local date (YYYY-MM-DD, or YYYY-MM-DDTHH:MM)")
    p.add_argument("--until", metavar="WHEN",
                   help="only files modified on or before this local date (YYYY-MM-DD, the whole day counts; "
                        "a YYYY-MM-DDTHH:MM time is exclusive; alone, the window has no start)")
    p.add_argument("--glob", default=GLOB, help=f"comma-separated file-name patterns (default {GLOB})")
    p.add_argument("--stdin", action="store_true", help="scan stdin instead, one command per line")
    p.add_argument("--json", action="store_true", help="one JSON object per hit, then a summary object")
    p.set_defaults(func=scan)


def scan(a):
    rules = hook.load_rules()
    if a.stdin:
        if a.paths:
            core.die(Exit.REFUSED, "--stdin takes no paths", EXAMPLES[2])
        lines = sys.stdin.read().splitlines()
        hits = [dict(h, file="-", line=n) for n, cmd in enumerate(lines, 1) if cmd.strip()
                for h in file_hits(cmd, rules)]
        return report(a, hits, {"commands_scanned": sum(1 for c in lines if c.strip())})
    lo, hi = core.window(a, "7d", WINDOW_EXAMPLE)
    files = list(walk(roots(a.paths), [g.strip() for g in a.glob.split(",") if g.strip()], lo, hi))
    hits, scanned, unreadable = [], 0, 0
    for f in files:
        try:
            st = f.stat()
            if st.st_size > MAX_FILE:
                continue
            data = f.read_bytes()
        except OSError:  # vanished or unreadable since the walk: counted, never fatal
            unreadable += 1
            continue
        if b"\0" in data[:8192]:
            continue
        scanned += 1
        mtime = datetime.datetime.fromtimestamp(st.st_mtime, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        hits += [dict(h, file=str(f), mtime=mtime) for h in file_hits(data.decode(errors="replace"), rules)]
    return report(a, hits, {"files_scanned": scanned, "unreadable": unreadable})


def roots(paths):
    """The paths to walk: the arguments, else the scan-dirs: line under ## gardener (relative = project root)."""
    if not paths:
        line = core.rules_line("gardener", "scan-dirs")
        f = core.find_project_file("project-rules.md")
        paths = [str(f.parents[1] / d) for d in core.names(line or "")] if f else []
        if not paths:
            core.die(Exit.REFUSED, "no paths, and no scan-dirs: line under ## gardener in .claude/project-rules.md",
                     EXAMPLES[0])
    for p in paths:
        if not os.path.exists(p):
            core.die(Exit.REFUSED, f"no such file or dir: {p}", EXAMPLES[0])
    return paths


def walk(paths, globs, lo, hi):
    """Files under paths whose name matches a glob and whose mtime is in [lo, hi) (sorted per dir)."""
    def keep(f):
        if not any(fnmatch.fnmatch(f.name, g) for g in globs):
            return False
        try:
            return lo <= f.stat().st_mtime < hi
        except OSError:
            return True  # vanished: the read below counts it as unreadable
    for p in map(Path, paths):
        if p.is_file():
            if keep(p):
                yield p
            continue
        for d, dirs, names in os.walk(p):
            dirs[:] = sorted(x for x in dirs if x not in SKIP_DIRS)
            for n in sorted(names):
                f = Path(d) / n
                if f.is_file() and keep(f):
                    yield f


def file_hits(text, rules):
    """[{rule, mode, line, bypassed}] for each rule that matches text (one hit per rule: its first match
    that survives `unless`).

    Unlike `hook pretooluse`, which applies `unless` to the whole command, scan applies it to each match's
    own command: through the next `done` for a while/until loop, else to the end of the line. A rule with
    `needs` is matched in windows of hook.MAX_CMD on each side of every `needs` hit, so a 1 MB file of near
    misses stays fast; a rule without `needs` sees the whole text.
    Known limit: a hit after a multi-line quoted string reports the line the string starts on.
    """
    nls = []
    code = hook.code_view(text, nls)
    bypass = set(hook.BYPASS.findall(text))
    cmd = re.compile(hook_rules.cmd_re(core.seam_lines("command-prefixes")))
    out = []
    for r in rules:
        if "needs" in r and not re.search(r["needs"], text):
            continue
        raw = r.get("on") == "raw"
        view = text if raw else code
        pos = first_hit(r, view, cmd)
        if pos is not None:
            at = pos if raw else view_to_raw(code, nls, pos)
            out.append({"rule": r["id"], "mode": r["mode"], "line": text.count("\n", 0, at) + 1,
                        "bypassed": r["id"] in bypass})
    return out


def first_hit(rule, view, cmd):
    """The view index of the command word of the rule's first surviving match, or None."""
    for start, end in windows(rule, view):
        chunk = view[start:end]
        for m in re.finditer(rule["match"], chunk):
            if "unless" in rule and re.search(rule["unless"], chunk[m.start():scope_end(chunk, m)]):
                continue
            c = cmd.match(chunk, m.start())  # skip the separator, keywords and VAR= words before the command
            return start + (c.end() if c and c.end() <= m.end() else m.start() + LEAD.match(m[0]).end())
    return None


def scope_end(chunk, m):
    """Where the match's command ends: the next `done` for a while/until loop, else the end of its line."""
    if LOOP.search(m[0]):
        d = DONE.search(chunk, m.end())
        return d.end() if d else len(chunk)
    nl = chunk.find("\n", m.end())
    return len(chunk) if nl < 0 else nl


def view_to_raw(code, nls, pos):
    """The raw index where the code-view line holding pos starts (nls: from hook.code_view)."""
    k = code.count("\n", 0, pos)
    return nls[k - 1] + 1 if k else 0


def windows(rule, view):
    """[(start, end)] slices of view to match in: merged MAX_CMD windows around each `needs` hit, each
    starting at a line start; the whole view when the rule has no `needs` or it never hits the view."""
    hits = [m.start() for m in re.finditer(rule["needs"], view)] if "needs" in rule else []
    if not hits:
        return [(0, len(view))]
    out = []
    for p in hits:
        start = view.rfind("\n", 0, max(0, p - hook.MAX_CMD)) + 1 if p > hook.MAX_CMD else 0
        end = min(len(view), p + hook.MAX_CMD)
        if out and start <= out[-1][1]:
            out[-1] = (out[-1][0], end)
        else:
            out.append((start, end))
    return out


def report(a, hits, summary):
    by_rule = {}
    for h in hits:
        by_rule[h["rule"]] = by_rule.get(h["rule"], 0) + 1
        row = {"file": h["file"], "rule": h["rule"], "mode": h["mode"], "line": h["line"],
               "mtime": h.get("mtime"), "bypassed": h["bypassed"]}
        core.out(a, row, f"{h['file']}:{h['line']}  {h['rule']} ({h['mode']})" + ("  bypassed" if h["bypassed"] else ""))
    summary.update(hits_by_rule=by_rule, bypassed=sum(h["bypassed"] for h in hits))
    core.emit(summary if a.json else dict(summary, hits_by_rule=json.dumps(by_rule)), a.json)
    return Exit.OK
