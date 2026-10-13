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
import time
from bisect import bisect_right
from pathlib import Path

from . import core, hook
from .core import Exit

GLOB = "*.sh,*.py,*.ts,*.mjs"
SKIP_DIRS = {"node_modules", ".venv", "venv", ".git", "evidence"}
MAX_FILE = 1024 * 1024
LEAD = re.compile(r"[\s;&|(]*")
EXAMPLES = ["agent-cli hook scan .claude/loop-evals --since 7d --json",
            "agent-cli hook scan scripts tools --glob '*.sh' --since 30d",
            "cat commands.txt | agent-cli hook scan --stdin --json",
            "agent-cli hook scan --since 7d --json   # the scan-dirs: line under ## gardener"]
DESCRIPTION = ("Scans each file under the paths (mtime within --since; skips node_modules, .venv, venv, .git,\n"
               "evidence/, binaries and files over 1 MB). With no paths it scans the comma-separated scan-dirs:\n"
               "line under `## gardener` in .claude/project-rules.md (relative to the project root).\n"
               "Prints one hit per file and rule {file, rule, mode, line, mtime, bypassed}, then a summary\n"
               "{files_scanned, hits_by_rule, bypassed}. A bypass comment counts as a hit, not as compliance.\n"
               "Exit 0 whenever the scan ran, whatever it found; 1 on a scan error; 2 on bad usage.")


def register(hs):
    p = core.subcommand(hs, "scan", "run every rule over script files or piped commands (read-only)", EXAMPLES)
    p.description = DESCRIPTION
    p.add_argument("paths", nargs="*", help="files or dirs to scan (default: scan-dirs from project-rules.md)")
    p.add_argument("--since", default="7d", help="only files modified within this long (default 7d)")
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
    since = core.duration(a.since, EXAMPLES[0])
    files = list(walk(roots(a.paths), [g.strip() for g in a.glob.split(",") if g.strip()], time.time() - since))
    hits, scanned = [], 0
    for f in files:
        try:
            st = f.stat()
            if st.st_size > MAX_FILE:
                continue
            data = f.read_bytes()
        except OSError as e:
            core.die(Exit.FAIL, f"cannot read {f}: {e}", EXAMPLES[0])
        if b"\0" in data[:8192]:
            continue
        scanned += 1
        mtime = datetime.datetime.fromtimestamp(st.st_mtime, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        hits += [dict(h, file=str(f), mtime=mtime) for h in file_hits(data.decode(errors="replace"), rules)]
    return report(a, hits, {"files_scanned": scanned})


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


def walk(paths, globs, cutoff):
    """Files under paths whose name matches a glob and whose mtime is at or after cutoff (sorted per dir)."""
    def keep(f):
        return any(fnmatch.fnmatch(f.name, g) for g in globs) and f.stat().st_mtime >= cutoff
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
    """[{rule, mode, line, bypassed}] for each rule that matches text (one hit per rule, its first match).

    A rule with `needs` is matched in windows of hook.MAX_CMD on each side of every `needs` hit, so a
    1 MB file of near-misses stays fast; a rule without `needs` sees the whole text.
    """
    nls = []
    code = hook.code_view(text, nls)
    bypass = set(hook.BYPASS.findall(text))
    out = []
    for r in rules:
        if "needs" in r and not re.search(r["needs"], text):
            continue
        raw = r.get("on") == "raw"
        view = text if raw else code
        bare = {k: v for k, v in r.items() if k != "needs"}
        for start, end in windows(r, view):
            m = hook.match(bare, view[start:end], view[start:end])
            if m:
                pos = start + m.start() + LEAD.match(m[0]).end()
                at = pos if raw else view_to_raw(code, nls, pos)
                out.append({"rule": r["id"], "mode": r["mode"], "line": text.count("\n", 0, at) + 1,
                            "bypassed": r["id"] in bypass})
                break
    return out


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
