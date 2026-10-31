#!/usr/bin/env python3
"""Save one implementer try as a folder, so later tries and the improver can read it.

Layout:
  <TRIES_ROOT>/<repo-name>/<bead-id>/try-<n>/
    start.sha  end.sha  diff.patch  gates.txt  transcript.jsonl (or transcript.missing)  note.md
  <TRIES_ROOT>/<repo-name>/index.tsv
    one row per try, no header:
    date  bead  try  result  files_changed  lines_changed  folder (relative to <repo-name>/)

TRIES_ROOT = $HARNESS_TRIES_DIR, default ~/.claude/tries.
<repo-name> = basename of the main repo root (the same for every worktree).

Usage:
  save_try.py --bead ID --worktree PATH --start SHA --gates FILE --note FILE --result pass|fail
              [--transcript PATH | --agent-id ID]
  save_try.py --bead ID --from-files DIR --repo-name NAME --result pass|fail [--note FILE]
              [--transcript PATH | --agent-id ID]
    (DIR holds start.sha, end.sha, diff.patch, gates.txt, and optionally
     transcript.jsonl and note.md; with no note, note.md says none was given)

Transcript: --transcript copies that file; --agent-id copies
$CLAUDE_CONFIG_DIR/projects/*/*/subagents/agent-<id>.jsonl (the id is in the Agent
tool result). With neither, it searches those subagent files made after the start
commit whose first user message names the bead, preferring implementers (per the
agent-<id>.meta.json beside each), then the earliest mention, then the newest; if
none, it writes transcript.missing describing the search. Prints the new folder path.

Every file written is passed through agent-cli's redactor (credentials become [REDACTED], a
password in a postgres URL becomes <REDACTED>) and set to mode 600, index.tsv included. The
count goes to stderr, never a value. A diff.patch that held a credential no longer applies cleanly. If redaction fails nothing is saved.
"""
import argparse
import datetime
import glob
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.dont_write_bytecode = True  # agent_cli lives in the repo being committed to
# .claude/skills/coordinator/save_try.py -> .claude/cli, where agent_cli lives (same layout when installed)
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "cli"))
from agent_cli.redact import redact_file  # noqa: E402


def git(wt, *args):
    return subprocess.run(["git", "-C", str(wt), *args], check=True,
                          text=True, capture_output=True).stdout


def patch_stats(patch):
    """(files_changed, lines_changed) counted from a unified git diff."""
    files = lines = 0
    in_hunk = False
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            files += 1
            in_hunk = False
        elif line.startswith("@@"):
            in_hunk = True
        elif in_hunk and line[:1] in ("+", "-"):
            lines += 1
    return files, lines


def first_user_text(path):
    """Text of the first user message in a transcript, or '' if none is readable."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                try:
                    d = json.loads(raw)
                except ValueError:
                    continue
                if d.get("type") != "user":
                    continue
                c = d.get("message", {}).get("content", "")
                if isinstance(c, list):
                    c = " ".join(x.get("text", "") for x in c if isinstance(x, dict))
                return c if isinstance(c, str) else ""
    except OSError:
        pass
    return ""


def agent_type(transcript):
    """agentType from the sibling agent-<id>.meta.json, or None if absent/unreadable."""
    try:
        return json.loads(Path(transcript).with_suffix(".meta.json").read_text()).get("agentType")
    except (OSError, ValueError, AttributeError):
        return None


def find_transcript(bead, agent_id, since):
    """Return (path or None, description of the search)."""
    projects = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")) / "projects"
    if agent_id:
        pattern = str(projects / "*/*/subagents" / f"agent-{agent_id}.jsonl")
        hits = sorted(glob.glob(pattern), key=os.path.getmtime)
        return (hits[-1] if hits else None), f"glob {pattern}: {len(hits)} match(es)"
    pattern = str(projects / "*/*/subagents/agent-*.jsonl")
    # The id must stand alone: "demo.3" must not match "demo.30" or "xdemo.3".
    rx = re.compile(r"(?<![\w.-])" + re.escape(bead) + r"(?![\w-]|\.\w)")
    candidates = [p for p in glob.glob(pattern) if os.path.getmtime(p) >= since]
    matches = [(m.start(), p) for p in candidates if (m := rx.search(first_user_text(p)))]
    # Reviewers and test-runners name the bead too; keep implementers when the
    # meta file says so (older transcripts have no meta file: keep everything).
    impl = [x for x in matches if agent_type(x[1]) == "implementer"]
    best = None
    for pos, p in impl or matches:
        # Prompts mention other beads too ("see x.2"), and another bead's implementer
        # can be newer, so the bead it works on (named first) wins. Ties (retries of
        # the same bead) go to the newest file.
        key = (pos, -os.path.getmtime(p))
        if best is None or key < best[0]:
            best = (key, p)
    desc = (f"glob {pattern}, modified at or after {since:.0f} (start commit time): "
            f"{len(candidates)} file(s); none had a first user message naming {bead}")
    return (best[1] if best else None), desc


def pick_transcript(a, since):
    """(transcript path or None, why it is missing). Exits if an explicit --transcript is absent."""
    if a.transcript:
        if not Path(a.transcript).is_file():
            sys.exit(f"save_try: transcript not found: {a.transcript}")
        return Path(a.transcript), None
    found, desc = find_transcript(a.bead, a.agent_id, since)
    return (Path(found), None) if found else (None, desc)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bead", required=True)
    ap.add_argument("--result", required=True, choices=["pass", "fail"])
    ap.add_argument("--worktree")
    ap.add_argument("--start")
    ap.add_argument("--gates")
    ap.add_argument("--note")
    ap.add_argument("--from-files")
    ap.add_argument("--repo-name")
    tx = ap.add_mutually_exclusive_group()
    tx.add_argument("--transcript")
    tx.add_argument("--agent-id")
    a = ap.parse_args()

    # Gather everything before creating the folder, so a bad input leaves nothing behind.
    if a.from_files:
        src = Path(a.from_files)
        if not a.repo_name:
            ap.error("--from-files needs --repo-name")
        note = Path(a.note) if a.note else src / "note.md"
        need = [src / f for f in ("start.sha", "end.sha", "diff.patch", "gates.txt")]
        if a.note:
            need.append(note)
        elif not note.is_file():
            note = None  # the exam's export has no note
        missing = [str(p) for p in need if not p.is_file()]
        if missing:
            sys.exit(f"save_try: missing input file(s): {', '.join(missing)}")
        start = (src / "start.sha").read_text().strip()
        end = (src / "end.sha").read_text().strip()
        patch = (src / "diff.patch").read_bytes()
        gates = src / "gates.txt"
        repo_name = a.repo_name
        if a.transcript or a.agent_id:
            transcript, missing_note = pick_transcript(a, since=0)
        elif (src / "transcript.jsonl").is_file():
            transcript, missing_note = src / "transcript.jsonl", None
        else:
            transcript, missing_note = None, f"no transcript.jsonl in {src} and no --transcript or --agent-id given"
    else:
        for opt in ("worktree", "start", "gates", "note"):
            if not getattr(a, opt):
                ap.error(f"--{opt} is required without --from-files")
        for p in (a.gates, a.note):
            if not Path(p).is_file():
                sys.exit(f"save_try: file not found: {p}")
        wt = Path(a.worktree)
        start = git(wt, "rev-parse", a.start + "^{commit}").strip()
        end = git(wt, "rev-parse", "HEAD").strip()
        patch = subprocess.run(["git", "-C", str(wt), "diff", "--binary", start, end],
                               check=True, capture_output=True).stdout
        gates, note = Path(a.gates), Path(a.note)
        common = Path(git(wt, "rev-parse", "--path-format=absolute", "--git-common-dir").strip())
        repo_name = a.repo_name or common.parent.name
        transcript, missing_note = pick_transcript(
            a, since=float(git(wt, "show", "-s", "--format=%ct", start).strip()))

    os.umask(0o077)  # nothing is ever group/world readable, not even before the redactor runs
    repo_dir = Path(os.environ.get("HARNESS_TRIES_DIR", Path.home() / ".claude/tries")) / repo_name
    bead_dir = repo_dir / a.bead
    bead_dir.mkdir(parents=True, exist_ok=True)
    nums = [int(m.group(1)) for d in bead_dir.iterdir() if (m := re.fullmatch(r"try-(\d+)", d.name))]
    n = max(nums, default=0) + 1
    while True:  # exclusive mkdir: two concurrent saves never share a number
        folder = bead_dir / f"try-{n}"
        try:
            folder.mkdir()
            break
        except FileExistsError:
            n += 1

    (folder / "start.sha").write_text(start + "\n")
    (folder / "end.sha").write_text(end + "\n")
    (folder / "diff.patch").write_bytes(patch)
    shutil.copyfile(gates, folder / "gates.txt")
    if note:
        shutil.copyfile(note, folder / "note.md")
    else:
        (folder / "note.md").write_text("No note was given for this try.\n")
    if transcript:
        shutil.copyfile(transcript, folder / "transcript.jsonl")
    else:
        (folder / "transcript.missing").write_text(f"bead {a.bead}: {missing_note}\n")

    try:
        redactions = sum(redact_file(f) for f in folder.iterdir())  # also chmod 600
    except OSError as e:  # fail closed: never leave a half-redacted try behind
        shutil.rmtree(folder, ignore_errors=True)
        sys.exit(f"save_try: redaction failed ({type(e).__name__}); nothing was saved")
    if redactions:
        print(f"save_try: redacted {redactions} credential(s) in {folder}", file=sys.stderr)

    files, lines = patch_stats(patch.decode("utf-8", errors="replace"))
    date = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    row = [date, a.bead, str(n), a.result, str(files), str(lines), f"{a.bead}/try-{n}"]
    with open(repo_dir / "index.tsv", "a") as fh:
        fh.write("\t".join(row) + "\n")
    os.chmod(repo_dir / "index.tsv", 0o600)
    print(folder)


if __name__ == "__main__":
    main()
