#!/usr/bin/env python3
"""Turn one merged PR of a local git repo into a Harbor task graded by that PR's own tests.

    build_from_pr.py --repo <git repo> --merge <sha> --test-file <path> [--test-file ...]
      --instruction <file> --test-command "<cmd>" --out <task dir> --id <id> --title <title>
      [--base-image node:24-bookworm] [--setup "<cmd>"] [--env KEY=VAL ...] [--workdir /app/repo]
      [--allow-host <host> ...] [--exclude <path> ...] [--cpus 2] [--memory-mb 4096]
      [--storage-mb 10240] [--verifier-no-network] [--force]
      [--manifest <json> --set <name> --source <text> --language <lang> --kind bugfix|feature
       --visible-test-command "<cmd>" --pr <N> --repo-slug <slug> --screen]

Writes <out>/:
  environment/repo.tar.gz   tree of <merge>^ minus the test files and --exclude paths
  environment/Dockerfile    FROM <base-image>, ENV per --env, unpacks the tarball into <workdir>, RUN <setup>
  instruction.md            copy of --instruction
  tests/files/<path>        each test file as of <merge> (the hidden tests)
  tests/test.sh             copies tests/files over <workdir>, runs --test-command, reward 1 iff exit 0
  solution/fix.patch        the PR's diff minus test files and excluded paths; solution/solve.sh applies it
  task.toml                 no network keys by default; --allow-host sets an agent allowlist,
                            --verifier-no-network a no-network verifier (opt-in: some sandbox
                            providers/tiers reject any per-sandbox network policy)

Refuses (non-zero, `build_from_pr: <reason>`, nothing written) when the snapshot
still holds a `.env` / `.env.*` file (exclude it), a test file is not a file at
<merge> or unchanged by the PR, the fix patch is empty, <out> is non-empty
without --force, --workdir is not `/` + [A-Za-z0-9._/-], --env/--setup/
--test-command holds a newline, or a --manifest check fails (--language/--kind
missing, --set/--source missing for a new manifest, the visible command naming
a hidden test file, or the resulting manifest failing manifest.check). Paths are git literal pathspecs (`app/[id]/x.ts` is no glob).
With --manifest, adds or replaces the row with this id (manifest.py schema;
test_command is the VISIBLE command, never the hidden one).
test.sh and solve.sh read TESTS_DIR, VERIFIER_DIR, SOLUTION_DIR and REPO_DIR
(default /tests, /logs/verifier, /solution, <workdir>) so they can run outside Harbor.
"""
import argparse
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parent))
import manifest  # noqa: E402

VERIFIER_TIMEOUT, AGENT_TIMEOUT, BUILD_TIMEOUT = 1800, 3600, 1800
ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
WORKDIR = re.compile(r"/[A-Za-z0-9._/-]+")


def die(msg):
    print(f"build_from_pr: {msg}", file=sys.stderr)
    sys.exit(2)


def git(repo, *args, ok=(0,)):
    p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True)
    if p.returncode not in ok:
        die(f"git {' '.join(args)} failed: {p.stderr.decode(errors='replace').strip()}")
    return p


def under(path, prefixes):
    p = PurePosixPath(path)
    return any(p == PurePosixPath(x) or PurePosixPath(x) in p.parents for x in prefixes)


def snapshot(repo, base, drop):
    """gzip tarball bytes of base's tree minus `drop` prefixes; dies on a remaining .env file."""
    src = tarfile.open(fileobj=io.BytesIO(git(repo, "archive", base).stdout))
    keep = [m for m in src.getmembers() if not under(m.name.rstrip("/"), drop)]
    for m in keep:
        name = PurePosixPath(m.name).name
        if not m.isdir() and (name == ".env" or name.startswith(".env.")):
            die(f"{m.name} would ship in the sandbox (possible secrets); --exclude {m.name}")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as out:
        for m in keep:
            out.addfile(m, src.extractfile(m) if m.isfile() else None)
    return buf.getvalue()


def toml_str(s):
    return json.dumps(s, ensure_ascii=False)  # a JSON string is a valid TOML basic string


def task_toml(a):
    lines = ['version = "1.0"', "", "[metadata]", f"name = {toml_str(a.id)}", f"title = {toml_str(a.title)}",
             "", "[verifier]", f"timeout_sec = {VERIFIER_TIMEOUT}.0"]
    if a.verifier_no_network:
        lines.append('network_mode = "no-network"')
    lines += ["", "[agent]", f"timeout_sec = {AGENT_TIMEOUT}.0"]
    if a.allow_host:
        lines += ['network_mode = "allowlist"',
                  f"allowed_hosts = [{', '.join(toml_str(h) for h in a.allow_host)}]"]
    lines += ["", "[environment]", f"build_timeout_sec = {BUILD_TIMEOUT}.0", f"cpus = {a.cpus}",
              f"memory_mb = {a.memory_mb}", f"storage_mb = {a.storage_mb}"]
    return "\n".join(lines) + "\n"


def docker_quote(v):
    """Escape a value for a double-quoted Dockerfile ENV: Docker expands $ and unescapes \\ and \"."""
    return re.sub(r'([$\\"])', r"\\\1", v)


def dockerfile(a, env):
    lines = [f"FROM {a.base_image}", *(f'ENV {k}="{docker_quote(v)}"' for k, v in env),
             f"WORKDIR {a.workdir}", "COPY repo.tar.gz /tmp/repo.tar.gz",
             f"RUN mkdir -p {a.workdir} && tar -xzf /tmp/repo.tar.gz -C {a.workdir} && rm /tmp/repo.tar.gz"]
    if a.setup:
        lines.append(f"RUN {a.setup}")
    return "\n".join(lines) + "\n"


def test_sh(a, env):
    exports = "".join(f"export {k}={shlex.quote(v)}\n" for k, v in env)
    return f"""#!/usr/bin/env bash
# Hidden-test verifier written by build_from_pr.py. Reward 1 iff the test command exits 0.
TESTS_DIR="${{TESTS_DIR:-/tests}}"
VERIFIER_DIR="${{VERIFIER_DIR:-/logs/verifier}}"
REPO_DIR="${{REPO_DIR:-{a.workdir}}}"
reward=0
mkdir -p "$VERIFIER_DIR"
trap 'echo "$reward" > "$VERIFIER_DIR/reward.txt"' EXIT
{exports}[ -d "$REPO_DIR" ] || {{ echo "workdir $REPO_DIR missing"; exit 1; }}
cp -a "$TESTS_DIR/files/." "$REPO_DIR/" || exit 1
cd "$REPO_DIR" || exit 1
bash -c {shlex.quote(a.test_command)} > "$VERIFIER_DIR/test-output.log" 2>&1 && reward=1
cat "$VERIFIER_DIR/test-output.log"
"""


def solve_sh(a):
    return f"""#!/usr/bin/env bash
set -euo pipefail
SOLUTION_DIR="${{SOLUTION_DIR:-/solution}}"
cd "${{REPO_DIR:-{a.workdir}}}"
git apply --binary "$SOLUTION_DIR/fix.patch"
"""


def manifest_row(a, out, mpath, merged_at):
    return {"id": a.id, "harbor_path": os.path.relpath(out, mpath.resolve().parent), "repo": a.repo_slug,
            "pr": a.pr, "merged_at": merged_at, "language": a.language, "kind": a.kind,
            "workdir": a.workdir, "test_command": a.visible_test_command, "title": a.title,
            "screen": a.screen}


def check_manifest_args(a, tests):
    """Every --manifest check that can run before anything is written."""
    for need in ("language", "kind"):
        if not getattr(a, need):
            die(f"--manifest needs --{need}")
    if not Path(a.manifest).exists() and not (a.set and a.source):
        die(f"{a.manifest} does not exist; --set and --source are needed to create it")
    named = manifest.leaked(a.visible_test_command or "", tests)
    if named:
        die(f"--visible-test-command names hidden test file(s) {', '.join(named)}")


def manifest_text(a, row):
    """The manifest with `row` added, checked by manifest.check before anything is written."""
    mpath = Path(a.manifest)
    try:
        data = json.loads(mpath.read_text()) if mpath.exists() else {"set": a.set, "source": a.source, "tasks": []}
        data["tasks"] = [t for t in data.get("tasks", []) if t.get("id") != a.id] + [row]
        text = json.dumps(data, indent=1) + "\n"
        manifest.check(json.loads(text), mpath)
    except (ValueError, AttributeError, TypeError) as e:
        die(f"manifest would be rejected, nothing written: {e}")
    return text


def parse(argv):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for flag in ("--repo", "--merge", "--instruction", "--test-command", "--out", "--id", "--title"):
        ap.add_argument(flag, required=True)
    ap.add_argument("--test-file", action="append", required=True)
    ap.add_argument("--base-image", default="node:24-bookworm")
    ap.add_argument("--setup")
    ap.add_argument("--env", action="append", default=[])
    ap.add_argument("--workdir", default="/app/repo")
    ap.add_argument("--allow-host", action="append", default=[])
    ap.add_argument("--exclude", action="append", default=[])
    ap.add_argument("--cpus", type=int, default=2)
    ap.add_argument("--memory-mb", type=int, default=4096)
    ap.add_argument("--storage-mb", type=int, default=10240)  # Daytona's per-sandbox disk cap
    ap.add_argument("--verifier-no-network", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--manifest")
    ap.add_argument("--set")
    ap.add_argument("--source")
    ap.add_argument("--language")
    ap.add_argument("--kind", choices=manifest.KINDS)
    ap.add_argument("--visible-test-command")
    ap.add_argument("--pr", type=int)
    ap.add_argument("--repo-slug")
    ap.add_argument("--screen", action="store_true")
    return ap.parse_args(argv)


def build(argv=None):
    a = parse(argv)
    repo, out = Path(a.repo), Path(a.out).resolve()
    env = []
    for kv in a.env:
        k, sep, v = kv.partition("=")
        if not sep or not ENV_KEY.fullmatch(k):
            die(f"--env must be KEY=VAL with a shell-safe KEY, got {kv!r}")
        env.append((k, v))
    for flag, values in (("--env", a.env), ("--setup", [a.setup or ""]), ("--test-command", [a.test_command])):
        if any("\n" in v or "\r" in v for v in values):
            die(f"{flag} must not contain a newline")
    if not WORKDIR.fullmatch(a.workdir) or PurePosixPath(a.workdir).parent == PurePosixPath("/"):
        die(f"--workdir must be / + [A-Za-z0-9._/-] and not directly under / "
            f"(its parent must stay writable): {a.workdir!r}")
    if out.exists() and any(out.iterdir()) and not a.force:
        die(f"{out} exists and is not empty; rerun with --force to replace it")
    merge = git(repo, "rev-parse", "--verify", f"{a.merge}^{{commit}}").stdout.decode().strip()
    base = f"{merge}^"
    tests = [PurePosixPath(t).as_posix() for t in a.test_file]
    if a.manifest:
        check_manifest_args(a, tests)

    # Compute everything before writing anything.
    files = {}
    for t in tests:
        kind = git(repo, "cat-file", "-t", f"{merge}:{t}", ok=(0, 128)).stdout.decode().strip()
        if kind != "blob":
            die(f"test file {t} is not a file at {merge[:12]} ({kind or 'missing'})")
        if not git(repo, "diff", "--quiet", base, merge, "--", f":(literal){t}", ok=(0, 1)).returncode:
            die(f"test file {t} is not changed by the PR ({merge[:12]}^..{merge[:12]})")
        files[t] = git(repo, "show", f"{merge}:{t}").stdout
    drop = tests + a.exclude
    tarball = snapshot(repo, base, drop)
    patch = git(repo, "-c", "diff.noprefix=false", "-c", "diff.mnemonicPrefix=false", "diff", "--no-color",
                "--no-ext-diff", "--binary", base, merge, "--", ".",
                *(f":(exclude,literal){p}" for p in drop)).stdout
    if not patch.strip():
        die("the PR changes nothing outside its test files and excluded paths; nothing to solve")
    instruction = Path(a.instruction).read_bytes()
    merged_at = git(repo, "log", "-1", "--format=%cs", merge).stdout.decode().strip()
    mtext = manifest_text(a, manifest_row(a, out, Path(a.manifest), merged_at)) if a.manifest else None

    if out.exists():
        shutil.rmtree(out)
    writes = {"environment/repo.tar.gz": tarball, "environment/Dockerfile": dockerfile(a, env),
              "instruction.md": instruction, "tests/test.sh": test_sh(a, env), "solution/fix.patch": patch,
              "solution/solve.sh": solve_sh(a), "task.toml": task_toml(a),
              **{f"tests/files/{t}": data for t, data in files.items()}}
    for rel, data in writes.items():
        p = out / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data.encode() if isinstance(data, str) else data)
        if rel.endswith(".sh"):
            p.chmod(0o755)
    if mtext:
        Path(a.manifest).parent.mkdir(parents=True, exist_ok=True)
        Path(a.manifest).write_text(mtext)
    print(f"build_from_pr: wrote {out} ({len(files)} hidden test file(s), fix.patch {len(patch)} bytes)")


if __name__ == "__main__":
    build()
