#!/usr/bin/env python3
"""Install the agent-workflow harness exactly as it was at one commit.

    install_harness.py --commit <ref> --target <dir> [--addon <dir>] [--repo <path>] [--force]

Installs the committed `.claude/` (minus `.claude/loop-evals/` and `.claude/cli/tests/`) and `AGENTS.md`
from <ref> via `git archive`, so a dirty worktree never leaks in. An optional
add-on dir is copied over `<target>/.claude/` (add-on files replace core files
at the same relative path). Then it writes:

  .claude/HARNESS_VERSION   {"commit": sha, "addon": path|null, "addon_sha256": hex|null}
  .claude/HARNESS_MANIFEST  every file this install wrote, one target-relative path per line
  .claude/HARNESS_FILES.tsv header `path\torigin\tsha256`, then one row per installed file
                            (the manifest minus these three files), sorted by path:
                            origin `addon` if the add-on supplied the path,
                            `base+addon` for settings.json when settings.add.json
                            was merged into it, else `base`; sha256 of the bytes on
                            disk after the install (so an edited settings.json
                            hashes the edited bytes).
                            `agent-cli harness drift` reads it.

"addon" is the add-on path relative to the target (posix, e.g.
`.claude/harness-addon`) when the add-on dir is inside the target, else the
path as given on the command line, so a committed HARNESS_VERSION carries no
machine-specific path.

addon_sha256 = sha256 over the add-on's files sorted by posix relpath, each fed
as `relpath \\0 len(bytes) \\0 bytes`.

An optional `<addon>/.installignore` lists add-on-relative path prefixes, one
per line (blank lines and `#` comments ignored, trailing `/` optional; a line
that is absolute, holds `..` or matches no file is refused). Files under them,
and `.installignore` itself, are neither installed nor hashed: a
private add-on dir may also hold private exam task folders (hidden tests and
the reference fix), which must never reach the agent's sandbox, and rebuilding
one must not change the harness version label. Any add-on file still to be
installed that is named `task.toml` (a task folder left un-ignored) is refused.

An optional `<addon>/.skipbase` opts the target out of parts of the base
(core) harness. It is never installed, but it IS hashed into addon_sha256
(fed like any add-on file, path `.skipbase`), so the version label changes
when the opt-outs do. One entry per line (blank lines and `#` comments ignored):

  path <prefix>            base files at or under this TARGET-relative prefix
                           (e.g. `.claude/skills/x/`, `AGENTS.md`) are not
                           installed; add-on files there still are
  setting <a.b.c>          delete this dotted key from the installed settings.json
  hook <Event> <matcher>   delete the hooks.<Event> groups whose "matcher" equals
                           <matcher> (`-` = groups with no "matcher" key); an Event left
                           with no groups is deleted

Fail closed: an unknown keyword, a malformed line, a path that is absolute,
holds `..` or matches no base file, a missing key, a hook entry matching no
group, or a setting/hook entry with no installed settings.json (or one that
is a symlink in the target, so it would be skipped) all exit 2
before anything is written.

An optional `<addon>/settings.add.json` ADDS to the installed settings.json
instead of replacing it (an add-on `settings.json` replaces it wholesale). Like
.skipbase it is never installed but IS hashed into addon_sha256 (path
`settings.add.json`). It applies after the .skipbase deletions, and the result
is written once:

  hooks.<Event>            each group is appended to the installed event's list
                           (the event is created when absent)
  any other key            dicts merge recursively, lists gain the items they
                           lack (in order), a scalar is added or replaces the
                           installed one

Fail closed, exit 2 before anything is written: an add-on shipping both
settings.json and settings.add.json; a file that is not a JSON object; `hooks`
that is not an object of Event -> non-empty list of groups each with a "hooks" list; a
group identical to one already in the installed event; a dict or list meeting
a different shape at the same key; or no installed settings.json (or one that
is a symlink in the target).

A previous manifest blocks a reinstall (exit 2) unless --force, which deletes
ONLY the files that manifest lists. Without a manifest, files are overlaid and
nothing is deleted. A path it would write through a symlink already in the
target (e.g. a skill dir linked into a nested repo) is skipped and left alone:
it is not written and not listed in the manifest, and a note names the link
and says whether the skipped content is identical to what is behind it (any
differing file is listed). Before touching anything it refuses if any
component of a path it would DELETE is a symlink, if HARNESS_VERSION,
HARNESS_MANIFEST or HARNESS_FILES.tsv would be written through one, or if a path lies under a
symlink shipped in the harness commit. Every refusal or failure exits 2 with
a message.
"""
import argparse
import hashlib
import io
import json
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

VERSION = ".claude/HARNESS_VERSION"
MANIFEST = ".claude/HARNESS_MANIFEST"
FILES = ".claude/HARNESS_FILES.tsv"
BOOKKEEPING = (VERSION, MANIFEST, FILES)
EXCLUDED = (".claude/loop-evals", ".claude/cli/tests")  # local eval data; agent-cli's own tests + corpus


def die(msg):
    print(f"install_harness: {msg}", file=sys.stderr)
    sys.exit(2)


def git(repo, *args):
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True)
    if result.returncode != 0:
        die(f"git {' '.join(args)} failed: {result.stderr.decode().strip()}")
    return result.stdout


IGNORE_FILE = ".installignore"
SKIP_FILE = ".skipbase"
ADD_FILE = "settings.add.json"
SETTINGS = ".claude/settings.json"


def addon_files(addon):
    """Sorted (posix relpath, Path) of every add-on file not ignored by .installignore.

    Dies on an ignore line that is absolute, holds `..` or matches no file, and
    on any file left to install that is named task.toml (an un-ignored task folder).
    """
    ignore = addon / IGNORE_FILE
    lines = ignore.read_text().splitlines() if ignore.is_file() else []
    patterns = [s for s in (ln.strip() for ln in lines) if s and not s.startswith("#")]
    for s in patterns:
        if s.startswith("/") or ".." in Path(s).parts:
            die(f"{ignore}: line {s!r} must be an add-on-relative prefix (no leading /, no ..)")
    skip = [Path(s.rstrip("/")) for s in patterns]
    rels = [p.relative_to(addon) for p in addon.rglob("*") if p.is_file()]
    for s, line in zip(skip, patterns):
        if not any(inside(r, s) for r in rels):
            die(f"{ignore}: line {line!r} matches no add-on file")
    skip += [Path(IGNORE_FILE), Path(SKIP_FILE), Path(ADD_FILE)]
    files = sorted((r.as_posix(), addon / r) for r in rels if not any(inside(r, s) for s in skip))
    for rel, _ in files:
        if Path(rel).name == "task.toml":
            die(f"add-on file {rel} looks like a private exam task folder; list it in {IGNORE_FILE}")
    return files


def addon_sha256(files):
    h = hashlib.sha256()
    for rel, p in files:
        data = p.read_bytes()
        h.update(rel.encode() + b"\0" + str(len(data)).encode() + b"\0" + data)
    return h.hexdigest()


LOCAL_FILE = re.compile(r"(^|/)(\.env(\.[^/]*)?|[^/]*\.bak|settings\.local\.json)$")


def installed_files(root):
    """Sorted (posix relpath, Path) of an already-installed harness tree at root: its .claude files plus
    AGENTS.md; in a git work tree the tracked and untracked ones git does not ignore (a working checkout's
    .gitignore keeps its local secrets and worktrees out), else every file (no __pycache__). run_exam.py
    --installed packs and hashes exactly these. ValueError naming the path (never the contents) when one is a
    .env / .env.* / *.bak / settings.local.json under .claude, and "no harness files under <root>" when none is
    under .claude (an empty listing would hash the same for every tree)."""
    root = Path(root)
    tracked = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard",
                              "--", ".claude", "AGENTS.md"], capture_output=True, text=True)
    if tracked.returncode == 0:
        files = [root / rel for rel in tracked.stdout.split("\0") if rel]
    else:
        files = [p for p in (root / ".claude").rglob("*") if "__pycache__" not in p.relative_to(root).parts]
        files.append(root / "AGENTS.md")
    out = sorted((p.relative_to(root).as_posix(), p) for p in files if p.is_file())
    if not any(rel.startswith(".claude/") for rel, _ in out):
        raise ValueError(f"no harness files under {root}: pass an installed .claude + AGENTS.md tree (in a git "
                         "work tree, .claude must not be ignored)")
    for rel, p in out:
        if rel.startswith(".claude/") and LOCAL_FILE.search(rel):
            raise ValueError(f"{p} is a local or secret file; an installed tree must not hold it (it would be "
                             "packed into the sandbox): remove it or pass a clean tree")
    return out


def installed_sha256(root):
    """addon_sha256's recipe over installed_files(root): the label of an installed tree."""
    return addon_sha256(installed_files(root))


def installed_version(root):
    """root/.claude/HARNESS_VERSION as a dict, or None when the tree has none (an agent-workflow checkout)."""
    f = Path(root) / VERSION
    return json.loads(f.read_text()) if f.is_file() else None


def inside(path, root):
    return path == root or root in path.parents


def read_skipbase(addon):
    """(paths, settings_ops) from <addon>/.skipbase, or ([], []) if absent. Dies on a malformed line."""
    f = addon / SKIP_FILE if addon else None
    if not f or not f.is_file():
        return [], []
    paths, ops = [], []
    for line in f.read_text().splitlines():
        words = line.split()
        if not words or words[0].startswith("#"):
            continue
        kind, rest = words[0], words[1:]
        if kind == "path" and len(rest) == 1:
            if rest[0].startswith("/") or ".." in Path(rest[0]).parts:
                die(f"{f}: line {line!r} must be a target-relative prefix (no leading /, no ..)")
            paths.append((line, Path(rest[0].rstrip("/"))))
        elif kind == "setting" and len(rest) == 1:
            ops.append((line, "setting", rest[0].split(".")))
        elif kind == "hook" and len(rest) == 2:
            ops.append((line, "hook", rest))
        else:
            die(f"{f}: cannot parse line {line!r} (want `path <prefix>`, `setting <a.b>` or `hook <Event> <matcher|->`)")
    return paths, ops


def apply_settings_ops(data, ops):
    """Delete the opted-out keys / hook groups from parsed settings; dies on any entry that matches nothing."""
    for line, kind, arg in ops:
        if kind == "setting":
            node = data
            for key in arg[:-1]:
                node = node.get(key) if isinstance(node, dict) else None
            if not isinstance(node, dict) or arg[-1] not in node:
                die(f"{SKIP_FILE}: line {line!r} names no key in {SETTINGS}")
            del node[arg[-1]]
        else:
            event, matcher = arg
            groups = data.get("hooks", {}).get(event) if isinstance(data.get("hooks"), dict) else None
            if not isinstance(groups, list):
                die(f"{SKIP_FILE}: line {line!r}: {SETTINGS} has no hooks.{event}")
            want = None if matcher == "-" else matcher
            kept = [g for g in groups if not (isinstance(g, dict) and g.get("matcher") == want)]
            if len(kept) == len(groups):
                die(f"{SKIP_FILE}: line {line!r} matches no hooks.{event} group")
            if kept:
                data["hooks"][event] = kept
            else:
                del data["hooks"][event]
    return data


def read_settings_add(addon):
    """The parsed <addon>/settings.add.json, or None if absent. Dies unless it is an object whose `hooks` (if any)
    maps each Event to a list of groups that each carry a "hooks" list."""
    f = addon / ADD_FILE if addon else None
    if not f or not f.is_file():
        return None
    try:
        data = json.loads(f.read_text())
    except ValueError as e:
        die(f"{f} is not valid JSON: {e}")
    if not isinstance(data, dict):
        die(f"{f} is not a JSON object")
    hooks = data.get("hooks", {})
    if not (isinstance(hooks, dict) and all(
            isinstance(gs, list) and gs
            and all(isinstance(g, dict) and isinstance(g.get("hooks"), list) for g in gs)
            for gs in hooks.values())):
        die(f'{f}: hooks must map each Event to a non-empty list of groups, each with a "hooks" list')
    return data


def merge_settings(data, add, path=()):
    """Merge settings.add.json into parsed settings in place (see the module doc); dies on a duplicate hook group
    or a shape conflict."""
    for key, value in add.items():
        here = ".".join((*path, key))
        if not path and key == "hooks":
            hooks = data.setdefault("hooks", {})
            for event, groups in value.items():
                have = hooks.setdefault(event, []) if isinstance(hooks, dict) else None
                if not isinstance(have, list):
                    die(f"{ADD_FILE}: shape conflict at hooks.{event}: {SETTINGS} holds no list of groups there")
                for g in groups:
                    if g in have:
                        die(f"{ADD_FILE}: hooks.{event} already has an identical group: {json.dumps(g)}")
                    have.append(g)
        elif key not in data:
            data[key] = value
        elif isinstance(data[key], dict) and isinstance(value, dict):
            merge_settings(data[key], value, (*path, key))
        elif isinstance(data[key], list) and isinstance(value, list):
            for item in value:
                if item not in data[key]:
                    data[key].append(item)
        elif isinstance(data[key], (dict, list)) or isinstance(value, (dict, list)):
            die(f"{ADD_FILE}: shape conflict at {here}: {type(data[key]).__name__} in {SETTINGS}, "
                f"{type(value).__name__} in {ADD_FILE}")
        else:
            data[key] = value
    return data


def first_symlink(target, rel):
    """The first path component of target/rel (dirs down to the file itself) that is a symlink, or None."""
    path = target
    for part in Path(rel).parts:
        path = path / part
        if path.is_symlink():
            return path
    return None


def install(argv):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--commit", required=True)
    ap.add_argument("--target", required=True)
    ap.add_argument("--addon")
    ap.add_argument("--repo", default=str(Path(__file__).resolve().parent.parent))
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args(argv)

    sha = git(args.repo, "rev-parse", "--verify", f"{args.commit}^{{commit}}").decode().strip()
    target = Path(args.target).resolve()

    addon = files = None
    if args.addon:
        addon = Path(args.addon).resolve()
        if not addon.is_dir():
            die(f"add-on dir not found: {addon}")
        files = addon_files(addon)
    skip_paths, settings_ops = read_skipbase(addon)
    settings_add = read_settings_add(addon)
    hashed = sorted([*(files or []),
                     *[(n, addon / n) for n in (SKIP_FILE, ADD_FILE) if addon and (addon / n).is_file()]])

    # Read the whole archive first: every guard runs before anything is written.
    tar = tarfile.open(fileobj=io.BytesIO(git(args.repo, "archive", sha, ".claude", "AGENTS.md")))
    members = [m for m in tar.getmembers()
               if not any(inside(Path(m.name), Path(x)) for x in EXCLUDED) and not m.isdir()]
    for line, prefix in skip_paths:
        hit = [m for m in members if inside(Path(m.name), prefix)]
        if not hit:
            die(f"{SKIP_FILE}: line {line!r} matches no base file")
        members = [m for m in members if m not in hit]
        print(f"opted out of base {prefix.as_posix()} ({len(hit)} files)")
    addon_by_rel = {f".claude/{rel}": src for rel, src in files or []}

    def source_bytes(rel):
        if rel in addon_by_rel:
            return addon_by_rel[rel].read_bytes()
        f = tar.extractfile(next(m for m in members if m.name == rel))
        return f.read() if f else None

    if settings_add is not None and SETTINGS in addon_by_rel:
        die(f"the add-on ships both settings.json and {ADD_FILE}; keep one (an add-on settings.json replaces "
            f"the base one, {ADD_FILE} adds to it)")
    edits = " and ".join([f"{SKIP_FILE} setting/hook lines"] * bool(settings_ops)
                         + [ADD_FILE] * (settings_add is not None))
    new_settings = None
    if edits:
        if SETTINGS not in addon_by_rel and SETTINGS not in {m.name for m in members}:
            die(f"{edits} cannot be applied: no {SETTINGS} is installed")
        raw = source_bytes(SETTINGS)
        data = json.loads(raw) if raw is not None else None
        if not isinstance(data, dict):
            die(f"{SETTINGS} is not a JSON object")
        data = apply_settings_ops(data, settings_ops)
        if settings_add is not None:
            data = merge_settings(data, settings_add)
        new_settings = (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode()
    planned = {m.name for m in members}
    planned |= {f".claude/{rel}" for rel, _ in files or []}
    planned |= set(BOOKKEEPING)

    old_manifest = target / MANIFEST
    previous = []
    if old_manifest.exists():
        if not args.force:
            die(f"{old_manifest} exists from a previous install; rerun with --force to replace it")
        previous = [line for line in old_manifest.read_text().splitlines() if line]
    for rel in previous:
        if not inside((target / rel).resolve(), target):
            die(f"previous manifest lists a path outside the target: {rel}")
    # Writing or deleting through a symlink would change files the manifest
    # does not own (e.g. skills linked into a nested repo): deletes refuse,
    # writes skip the linked path and leave it as it is.
    core_links = {m.name for m in members if m.issym() or m.islnk()}
    for rel in previous:
        link = first_symlink(target, rel)
        if link:
            die(f"refusing to delete through a symlink: {link}")
    linked = {}  # symlink in the target -> planned paths under it, left alone
    for rel in sorted(planned):
        link = first_symlink(target, rel)
        if link and rel in BOOKKEEPING:
            die(f"refusing to write {rel} through a symlink: {link}")
        if link and rel == SETTINGS and new_settings is not None:
            die(f"{edits} cannot be applied: {SETTINGS} is a symlink ({link})")
        if link:
            linked.setdefault(link, []).append(rel)
    for rel in planned:
        if any(a.as_posix() in core_links for a in Path(rel).parents):
            die(f"refusing to write through a symlink in the harness itself: {rel}")
    for link, rels in linked.items():
        differ = [rel for rel in rels
                  if not (target / rel).is_file() or (target / rel).read_bytes() != source_bytes(rel)]
        state = (f"{len(differ)} of {len(rels)} files differ from what is behind it: {', '.join(differ)}"
                 if differ else f"all {len(rels)} files identical to what is behind it")
        print(f"skipped symlink {link.relative_to(target).as_posix()} (left in place; {state})")
        planned -= set(rels)
    members = [m for m in members if m.name in planned]
    if addon:
        for rel in [*previous, *planned]:
            if inside((target / rel).resolve(), addon):
                die(f"refusing to touch the add-on dir: {rel} resolves inside {addon}")

    # The old manifest is never deleted here; it is rewritten below.
    for rel in previous:
        path = target / rel
        if rel == MANIFEST:
            continue
        if path.is_file():
            path.unlink()
        parent = path.parent
        while parent != target and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent

    for rel in sorted(planned):
        if (target / rel).exists() and rel not in previous:
            print(f"replaced {rel}")

    # Track every path this run may write before writing any, so a failure
    # mid-install leaves nothing untracked; on success it shrinks to `planned`.
    (target / MANIFEST).parent.mkdir(parents=True, exist_ok=True)
    (target / MANIFEST).write_text("".join(f"{rel}\n" for rel in sorted(set(previous) | planned)))
    tar.extractall(target, members=members, filter="data")
    for rel, src in files or []:
        if f".claude/{rel}" not in planned:
            continue
        dest = target / ".claude" / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, dest)  # keeps the executable bit for add-on hooks
    if new_settings is not None and SETTINGS in planned:
        (target / SETTINGS).write_bytes(new_settings)

    def origin(rel):
        return ("addon" if rel in addon_by_rel else
                "base+addon" if rel == SETTINGS and settings_add is not None else "base")
    rows = "".join(f"{rel}\t{origin(rel)}\t"
                   f"{hashlib.sha256((target / rel).read_bytes()).hexdigest()}\n"
                   for rel in sorted(planned - set(BOOKKEEPING)))
    (target / FILES).write_text("path\torigin\tsha256\n" + rows)
    label = addon.relative_to(target).as_posix() if addon and inside(addon, target) else args.addon
    version = {"commit": sha, "addon": label,
               "addon_sha256": addon_sha256(hashed) if addon else None}
    (target / VERSION).write_text(json.dumps(version) + "\n")
    (target / MANIFEST).write_text("".join(f"{rel}\n" for rel in sorted(planned)))

    print(f"installed agent-workflow {sha} into {target / '.claude'} "
          f"({len(planned) - len(BOOKKEEPING)} files), addon: {addon or 'none'}")


def main(argv=None):
    try:
        install(argv)
    except (OSError, tarfile.TarError, ValueError) as e:  # ValueError covers bad JSON and UnicodeDecodeError
        die(f"install failed: {e}")

if __name__ == "__main__":
    main()
